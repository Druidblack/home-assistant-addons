"""
SMS Gammu Gateway - Support functions
Gammu integration functions for SMS operations and state machine management

Based on: https://github.com/pajikos/sms-gammu-gateway
Licensed under Apache License 2.0
"""

import sys
import os
import logging
import threading
import time
import gammu


class GammuStateManager:
    """Small proxy around gammu.StateMachine with full reconnect support.

    The rest of the add-on can keep using this object like a regular
    gammu.StateMachine: machine.GetSignalQuality(), machine.SendSMS(), etc.
    When reinitialize() is called, the underlying StateMachine is replaced,
    while all existing references to this proxy stay valid.
    """

    def __init__(
        self,
        pin=None,
        device_path='/dev/ttyUSB0',
        connection='at',
        commtimeout=30,
        config_file='/tmp/gammu.config',
        init_retries=1,
        init_retry_delay=5,
    ):
        self.pin = pin
        self.device_path = device_path
        self.connection = connection or 'at'
        self.commtimeout = int(commtimeout or 30)
        self.config_file = config_file
        self.init_retries = max(1, int(init_retries or 1))
        self.init_retry_delay = max(1, int(init_retry_delay or 5))
        self._lock = threading.RLock()
        self._machine = None
        self._write_config()
        self.reinitialize(reason='initial startup')

    def _write_config(self):
        config_content = f"""[gammu]
device = {self.device_path}
connection = {self.connection}
commtimeout = {self.commtimeout}
"""
        with open(self.config_file, 'w') as f:
            f.write(config_content)
        logging.info(
            "Gammu config: device=%s, connection=%s, commtimeout=%ss",
            self.device_path,
            self.connection,
            self.commtimeout,
        )

    def _create_machine(self):
        sm = gammu.StateMachine()
        sm.ReadConfig(Filename=self.config_file)
        sm.Init()
        logging.info("Successfully initialized gammu with device: %s", self.device_path)

        # Try to check security status. Some modems/SIM states do not support it.
        try:
            security_status = sm.GetSecurityStatus()
            logging.info("SIM security status: %s", security_status)

            if security_status == 'PIN':
                if self.pin is None or self.pin == '':
                    logging.error("PIN is required but not provided.")
                    sys.exit(1)
                sm.EnterSecurityCode('PIN', self.pin)
                logging.info("PIN entered successfully")
        except Exception as e:
            logging.warning("Could not check SIM security status: %s", e)

        return sm

    def reinitialize(self, reason='manual reinitialize'):
        """Terminate the current StateMachine and create a fresh one."""
        with self._lock:
            logging.warning("🔄 Reinitializing Gammu state machine (%s)...", reason)
            old_machine = self._machine
            if old_machine is not None:
                try:
                    old_machine.Terminate()
                    time.sleep(1)
                except Exception as e:
                    logging.debug("Ignoring Terminate() error during reinitialize: %s", e)

            last_error = None
            for attempt in range(1, self.init_retries + 1):
                try:
                    self._machine = self._create_machine()
                    logging.info("✅ Gammu state machine is ready")
                    return self._machine
                except gammu.ERR_NOSIM:
                    # Keep behavior compatible with the previous version: the modem can be
                    # reachable even when SIM is temporarily inaccessible.
                    logging.warning("SIM card not accessible, but device is connected")
                    self._machine = old_machine
                    return self._machine
                except Exception as e:
                    last_error = e
                    logging.error(
                        "Error initializing device on attempt %s/%s: %s",
                        attempt,
                        self.init_retries,
                        e,
                    )
                    if attempt < self.init_retries:
                        time.sleep(self.init_retry_delay)

            try:
                devices = [d for d in os.listdir('/dev/') if d.startswith('tty')]
                logging.info(
                    "Available devices: %s",
                    ', '.join([f'/dev/{d}' for d in sorted(devices)[:30]]),
                )
            except Exception:
                pass
            raise last_error

    def __getattr__(self, name):
        machine = self._machine
        if machine is None:
            raise RuntimeError('Gammu state machine is not initialized')
        return getattr(machine, name)


def init_state_machine(
    pin,
    device_path='/dev/ttyUSB0',
    connection='at',
    commtimeout=30,
    init_retries=1,
    init_retry_delay=5,
):
    """Initialize gammu state manager with HA add-on config."""
    return GammuStateManager(
        pin=pin,
        device_path=device_path,
        connection=connection,
        commtimeout=commtimeout,
        init_retries=init_retries,
        init_retry_delay=init_retry_delay,
    )


def retrieveAllSms(machine):
    """Retrieve all SMS messages from SIM/device memory."""
    try:
        status = machine.GetSMSStatus()
        allMultiPartSmsCount = status['SIMUsed'] + status['PhoneUsed'] + status['TemplatesUsed']

        allMultiPartSms = []
        start = True

        while len(allMultiPartSms) < allMultiPartSmsCount:
            if start:
                currentMultiPartSms = machine.GetNextSMS(Start=True, Folder=0)
                start = False
            else:
                currentMultiPartSms = machine.GetNextSMS(Location=currentMultiPartSms[0]['Location'], Folder=0)
            allMultiPartSms.append(currentMultiPartSms)

        allSms = gammu.LinkSMS(allMultiPartSms)

        results = []
        for sms in allSms:
            smsPart = sms[0]

            result = {
                "Date": str(smsPart['DateTime']),
                "Number": smsPart['Number'],
                "State": smsPart['State'],
                "Locations": [smsPart['Location'] for smsPart in sms],
            }

            # Try to decode SMS - this may fail for MMS notifications or corrupted messages.
            try:
                decodedSms = gammu.DecodeSMS(sms)
                if decodedSms is None:
                    result["Text"] = smsPart.get('Text', '')
                else:
                    text = ""
                    for entry in decodedSms['Entries']:
                        if entry.get('Buffer') is not None:
                            text += entry['Buffer']
                    result["Text"] = text if text else smsPart.get('Text', '')

            except UnicodeDecodeError as e:
                logging.warning("Cannot decode SMS as UTF-8 (probably MMS notification): %s", e)
                try:
                    raw_text = smsPart.get('Text', '')
                    if isinstance(raw_text, bytes):
                        result["Text"] = raw_text.decode('utf-8', errors='replace')
                    else:
                        result["Text"] = str(raw_text) if raw_text else '[MMS or binary message]'
                except Exception:
                    result["Text"] = '[MMS or binary message - cannot display]'

            except Exception as e:
                logging.warning("Error decoding SMS: %s", e)
                try:
                    raw_text = smsPart.get('Text', '')
                    if isinstance(raw_text, bytes):
                        result["Text"] = raw_text.decode('utf-8', errors='replace')
                    else:
                        result["Text"] = str(raw_text) if raw_text else '[Decoding error]'
                except Exception:
                    result["Text"] = '[Message decoding failed]'

            results.append(result)

        return results

    except Exception as e:
        logging.error("Error retrieving SMS: %s", e)
        raise


def deleteSms(machine, sms):
    """Delete SMS by location. Errors are re-raised so callers can track failures."""
    try:
        for location in sms.get("Locations", []):
            machine.DeleteSMS(Folder=0, Location=location)
    except Exception as e:
        logging.error("Error deleting SMS: %s", e)
        raise


def encodeSms(smsinfo):
    """Encode SMS for sending."""
    return gammu.EncodeSMS(smsinfo)


def setupCallbacks(machine, unified_callback):
    """
    Set callback for incoming calls and SMS.

    Args:
        machine: Gammu state machine or GammuStateManager proxy
        unified_callback: callback function for all events (sm, event_type, data)

    Returns: {'calls': bool, 'sms': bool}
    """
    result = {'calls': False, 'sms': False}

    try:
        machine.SetIncomingCallback(unified_callback)
        logging.info("📱 Unified callback: SetIncomingCallback registered")
    except Exception as e:
        logging.error("📱 SetIncomingCallback failed: %s: %s", type(e).__name__, e)
        return result

    try:
        machine.SetIncomingCall()
        result['calls'] = True
        logging.info("📞 Call notifications: ENABLED")
    except gammu.ERR_NOTSUPPORTED:
        logging.warning("📞 SetIncomingCall: Not supported by this modem")
    except Exception as e:
        logging.error("📞 SetIncomingCall failed: %s: %s", type(e).__name__, e)

    try:
        machine.SetIncomingSMS()
        result['sms'] = True
        logging.info("📨 SMS notifications: ENABLED")
    except gammu.ERR_NOTSUPPORTED:
        logging.warning("📨 SetIncomingSMS: Not supported by this modem")
    except Exception as e:
        logging.error("📨 SetIncomingSMS failed: %s: %s", type(e).__name__, e)

    return result
