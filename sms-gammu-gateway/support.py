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
import multiprocessing
import queue
import uuid
import gammu




class GammuWorkerError(Exception):
    """Error returned by the isolated Gammu worker process."""


class GammuOperationTimeout(TimeoutError):
    """Raised when a Gammu operation does not finish in time."""


def _write_gammu_config(config_file, device_path, connection, commtimeout):
    config_content = f"""[gammu]
device = {device_path}
connection = {connection}
commtimeout = {commtimeout}
"""
    with open(config_file, 'w') as f:
        f.write(config_content)




def _connection_baudrate(connection):
    """Infer serial baudrate from Gammu connection name (at115200, at9600, ...)."""
    connection = str(connection or 'at')
    if connection.startswith('at') and len(connection) > 2:
        suffix = connection[2:]
        if suffix.isdigit():
            return int(suffix)
    # Huawei USB modems usually accept 115200 on their AT port even when Gammu
    # uses the generic autobaud "at" connection.
    return 115200


def _read_serial_response(ser, timeout):
    """Read a compact AT response without blocking forever."""
    deadline = time.time() + max(0.5, float(timeout or 5))
    chunks = []
    while time.time() < deadline:
        try:
            waiting = getattr(ser, 'in_waiting', 0)
            data = ser.read(waiting or 1)
        except Exception:
            break
        if data:
            try:
                text = data.decode('utf-8', errors='replace')
            except Exception:
                text = repr(data)
            chunks.append(text)
            joined = ''.join(chunks)
            if '\nOK' in joined or '\rOK' in joined or 'ERROR' in joined or '+CME ERROR' in joined or '+CMS ERROR' in joined:
                break
        else:
            time.sleep(0.05)
    return ''.join(chunks).strip()


def _run_raw_at_sequence(device_path, connection, commands, at_timeout=5, command_delay=0.5, reason='raw AT sequence'):
    """Run raw AT commands outside python-gammu.

    This is used only for recovery modes where Gammu status commands keep
    working but the modem/network stops delivering MT-SMS until a deeper AT
    interaction happens. The owning Gammu StateMachine must be terminated before
    calling this helper.
    """
    import serial

    baudrate = _connection_baudrate(connection)
    results = []
    logging.warning("📟 Raw AT recovery started (%s): device=%s baudrate=%s", reason, device_path, baudrate)

    with serial.Serial(device_path, baudrate=baudrate, timeout=0.3, write_timeout=max(1, int(at_timeout or 5))) as ser:
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception:
            pass

        for item in commands:
            if isinstance(item, dict):
                sleep_for = float(item.get('sleep', 0) or 0)
                if sleep_for > 0:
                    logging.info("📟 Raw AT recovery sleep %.1fs", sleep_for)
                    time.sleep(sleep_for)
                continue

            command = str(item).strip()
            if not command:
                continue
            wire = (command + '\r').encode('ascii', errors='ignore')
            logging.info("📟 AT > %s", command)
            ser.write(wire)
            ser.flush()
            response = _read_serial_response(ser, at_timeout)
            if response:
                compact = ' | '.join(line.strip() for line in response.replace('\r', '\n').split('\n') if line.strip())
                logging.info("📟 AT < %s", compact[:500])
            else:
                logging.info("📟 AT < <no response>")
            results.append({'command': command, 'response': response})
            if command_delay > 0:
                time.sleep(command_delay)

    logging.warning("✅ Raw AT recovery completed (%s)", reason)
    return results


def build_sms_wake_commands(mode):
    """Build conservative AT recovery sequences for stuck MT-SMS delivery."""
    mode = str(mode or '').lower()
    sms_init = [
        'AT',
        'ATE0',
        'AT+CMEE=2',
        'AT+CPIN?',
        'AT+CMGF=1',
        # Force SIM storage. Huawei sticks can otherwise keep messages in a
        # storage Gammu does not scan consistently after voice/network events.
        'AT+CPMS="SM","SM","SM"',
        'AT+CPMS?',
        # Huawei/Gammu issue #486 shows E3531 users sometimes need CNMI
        # 2,0,0,2,1 instead of the Gammu default 2,1,0,2.
        'AT+CNMI=2,0,0,2,1',
        'AT+CNMI?',
        'AT+CSQ',
        'AT+CREG?',
    ]
    if mode in ('at_wake', 'cnmi', 'sms_init'):
        return sms_init
    if mode in ('network_wake', 'cfun', 'radio'):
        return [
            'AT',
            'ATE0',
            'AT+CMEE=2',
            'AT+CFUN?',
            'AT+CFUN=0',
            {'sleep': 8},
            'AT+CFUN=1',
            {'sleep': 20},
            'AT+CPIN?',
            'AT+CREG?',
            'AT+CSQ',
        ] + sms_init[4:]
    return sms_init

def _create_raw_state_machine(settings):
    """Create and initialize a raw gammu.StateMachine inside the owning process."""
    pin = settings.get('pin')
    device_path = settings.get('device_path', '/dev/ttyUSB0')
    connection = settings.get('connection', 'at') or 'at'
    commtimeout = int(settings.get('commtimeout', 30) or 30)
    config_file = settings.get('config_file', '/tmp/gammu_worker.config')

    _write_gammu_config(config_file, device_path, connection, commtimeout)
    sm = gammu.StateMachine()
    sm.ReadConfig(Filename=config_file)
    sm.Init()

    try:
        security_status = sm.GetSecurityStatus()
        logging.info("SIM security status: %s", security_status)
        if security_status == 'PIN':
            if not pin:
                raise RuntimeError("PIN is required but not provided")
            sm.EnterSecurityCode('PIN', pin)
            logging.info("PIN entered successfully")
    except Exception as e:
        # Keep compatibility with the previous add-on behavior: many modems do not
        # support security status queries reliably, but can still send/read SMS.
        logging.warning("Could not check SIM security status: %s", e)

    return sm


def _gammu_process_worker(settings, request_queue, response_queue):
    """Own all python-gammu calls in a child process.

    This is intentionally small and boring: one StateMachine, one command at a
    time. If libgammu blocks forever in a C/serial call, the parent can kill this
    whole process and start a fresh one without freezing Flask/MQTT threads.
    """
    machine = None

    def ensure_machine():
        nonlocal machine
        if machine is None:
            machine = _create_raw_state_machine(settings)
            logging.info(
                "Successfully initialized isolated gammu worker with device: %s",
                settings.get('device_path', '/dev/ttyUSB0'),
            )
        return machine

    def terminate_machine():
        nonlocal machine
        if machine is not None:
            try:
                machine.Terminate()
            except Exception as e:
                logging.debug("Ignoring Gammu Terminate() error in worker: %s", e)
            machine = None

    while True:
        request_id = None
        try:
            request = request_queue.get()
            request_id = request.get('id')
            operation = request.get('operation')
            args = request.get('args', ())
            kwargs = request.get('kwargs', {})

            if operation in ('_stop', 'Terminate'):
                terminate_machine()
                response_queue.put({'id': request_id, 'ok': True, 'result': True})
                break

            if operation == '_ping':
                ensure_machine()
                response_queue.put({'id': request_id, 'ok': True, 'result': True})
                continue

            if operation == 'reinitialize':
                terminate_machine()
                time.sleep(1)
                ensure_machine()
                response_queue.put({'id': request_id, 'ok': True, 'result': True})
                continue

            if operation == '_raw_at_sequence':
                commands = args[0] if args else []
                terminate_machine()
                result = _run_raw_at_sequence(
                    settings.get('device_path', '/dev/ttyUSB0'),
                    settings.get('connection', 'at'),
                    commands,
                    at_timeout=kwargs.get('at_timeout', 5),
                    command_delay=kwargs.get('command_delay', 0.5),
                    reason=kwargs.get('reason', 'raw AT recovery'),
                )
                # Recreate Gammu immediately after raw AT so the next normal
                # operation does not have to pay initialization cost.
                ensure_machine()
                response_queue.put({'id': request_id, 'ok': True, 'result': result})
                continue

            sm = ensure_machine()
            result = getattr(sm, operation)(*args, **kwargs)
            response_queue.put({'id': request_id, 'ok': True, 'result': result})

        except Exception as e:
            # Do not try to pickle gammu exception objects directly; pickle a
            # compact error payload instead.
            try:
                response_queue.put({
                    'id': request_id,
                    'ok': False,
                    'error': str(e),
                    'error_type': type(e).__name__,
                    'error_repr': repr(e),
                })
            except Exception:
                pass


class GammuProcessClient:
    """Process-isolated proxy compatible with gammu.StateMachine method calls."""

    is_process_worker = True

    def __init__(
        self,
        pin=None,
        device_path='/dev/ttyUSB0',
        connection='at',
        commtimeout=30,
        operation_timeout=90,
        config_file='/tmp/gammu_worker.config',
    ):
        self.pin = pin
        self.device_path = device_path
        self.connection = connection or 'at'
        self.commtimeout = int(commtimeout or 30)
        self.operation_timeout = int(operation_timeout or 90)
        self.config_file = config_file
        self._manager_lock = threading.RLock()
        self._ctx = multiprocessing.get_context('fork') if 'fork' in multiprocessing.get_all_start_methods() else multiprocessing.get_context()
        self._request_queue = None
        self._response_queue = None
        self._process = None
        self._start_worker(reason='initial startup')

    def _settings(self):
        return {
            'pin': self.pin,
            'device_path': self.device_path,
            'connection': self.connection,
            'commtimeout': self.commtimeout,
            'config_file': self.config_file,
        }

    def _start_worker(self, reason='restart'):
        with self._manager_lock:
            self._stop_worker(kill=True)
            self._request_queue = self._ctx.Queue()
            self._response_queue = self._ctx.Queue()
            self._process = self._ctx.Process(
                target=_gammu_process_worker,
                args=(self._settings(), self._request_queue, self._response_queue),
                daemon=True,
                name='GammuProcessWorker',
            )
            self._process.start()
            logging.warning(
                "🔄 Started isolated Gammu worker process pid=%s (%s), timeout=%ss, device=%s, connection=%s",
                self._process.pid,
                reason,
                self.operation_timeout,
                self.device_path,
                self.connection,
            )

    def _stop_worker(self, kill=False):
        proc = self._process
        if proc is None:
            return
        try:
            if proc.is_alive() and not kill and self._request_queue is not None:
                request_id = str(uuid.uuid4())
                self._request_queue.put({'id': request_id, 'operation': '_stop', 'args': (), 'kwargs': {}})
                proc.join(timeout=5)
        except Exception:
            pass
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
        if proc.is_alive():
            try:
                proc.kill()
                proc.join(timeout=5)
            except Exception:
                pass
        self._process = None
        self._request_queue = None
        self._response_queue = None

    def _restart_after_timeout(self, operation):
        logging.error(
            "⏱️ Gammu operation '%s' timed out after %ss; killing isolated worker process",
            operation,
            self.operation_timeout,
        )
        self._start_worker(reason=f"timeout in {operation}")

    def call(self, operation, *args, timeout=None, **kwargs):
        timeout = int(timeout or self.operation_timeout)
        with self._manager_lock:
            if self._process is None or not self._process.is_alive():
                self._start_worker(reason='worker not running')

            request_id = str(uuid.uuid4())
            self._request_queue.put({
                'id': request_id,
                'operation': operation,
                'args': args,
                'kwargs': kwargs,
            })

            deadline = time.time() + timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    self._restart_after_timeout(operation)
                    raise GammuOperationTimeout(
                        f"Gammu operation '{operation}' timed out after {timeout}s"
                    )

                try:
                    response = self._response_queue.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    if self._process is None or not self._process.is_alive():
                        self._start_worker(reason=f'worker exited during {operation}')
                        raise GammuWorkerError(
                            f"Gammu worker exited during operation '{operation}'"
                        )
                    continue

                if response.get('id') != request_id:
                    # The main process serializes operations, so this should not normally happen.
                    logging.warning("Ignoring stale Gammu worker response for request %s", response.get('id'))
                    continue

                if response.get('ok'):
                    return response.get('result')

                error = response.get('error') or response.get('error_repr') or 'Unknown Gammu worker error'
                raise GammuWorkerError(error)

    def reinitialize(self, reason='manual reinitialize'):
        logging.warning("🔄 Reinitializing isolated Gammu worker (%s)...", reason)
        try:
            return self.call('reinitialize', timeout=max(self.operation_timeout, self.commtimeout + 30))
        except Exception as e:
            logging.warning("Worker reinitialize command failed, starting fresh process: %s", e)
            self._start_worker(reason=reason)
            return self.call('_ping', timeout=max(self.operation_timeout, self.commtimeout + 30))

    def RawATSequence(self, commands, reason='raw AT recovery', at_timeout=5, command_delay=0.5):
        timeout = max(self.operation_timeout, int(at_timeout or 5) * max(1, len(commands)) + 45)
        return self.call(
            '_raw_at_sequence',
            commands,
            timeout=timeout,
            reason=reason,
            at_timeout=at_timeout,
            command_delay=command_delay,
        )

    def Terminate(self):
        self._stop_worker(kill=False)
        return True

    def terminate(self):
        return self.Terminate()

    def __getattr__(self, name):
        def _method(*args, **kwargs):
            return self.call(name, *args, **kwargs)
        return _method


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

    def RawATSequence(self, commands, reason='raw AT recovery', at_timeout=5, command_delay=0.5):
        with self._lock:
            old_machine = self._machine
            if old_machine is not None:
                try:
                    old_machine.Terminate()
                    time.sleep(1)
                except Exception as e:
                    logging.debug("Ignoring Terminate() error before raw AT sequence: %s", e)
                self._machine = None

            result = _run_raw_at_sequence(
                self.device_path,
                self.connection,
                commands,
                at_timeout=at_timeout,
                command_delay=command_delay,
                reason=reason,
            )
            self.reinitialize(reason=f"after {reason}")
            return result

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
    process_worker=True,
    operation_timeout=90,
):
    """Initialize Gammu access for HA add-on config.

    process_worker=True uses a child process that owns the modem. This is
    safer than calling python-gammu directly from Flask/MQTT threads because a
    blocked C/serial call can be killed by restarting the child process.
    """
    if process_worker:
        return GammuProcessClient(
            pin=pin,
            device_path=device_path,
            connection=connection,
            commtimeout=commtimeout,
            operation_timeout=operation_timeout,
        )

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
