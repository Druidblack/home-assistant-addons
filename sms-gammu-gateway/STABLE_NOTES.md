# SMS Gammu Gateway stable build notes

This build focuses on modem stability for Home Assistant add-on usage.

## Main changes

- Reconnectable `GammuStateManager` proxy in `support.py`.
  Existing code still uses `machine.GetSignalQuality()`, `machine.SendSMS()`, etc., but the underlying `gammu.StateMachine` can now be fully replaced during recovery.
- Removed the ineffective `ThreadPoolExecutor.result(timeout=60)` watchdog in `mqtt_publisher.py`.
  The previous executor context could still wait forever for a stuck worker when leaving the `with` block.
- Added automatic full Gammu reinitialization after repeated failures:
  - `Terminate()` old session when possible
  - create a fresh `gammu.StateMachine()`
  - `ReadConfig()`
  - `Init()`
  - re-register callbacks when callback monitoring is enabled
- Changed default `missed_calls_monitoring_enabled` to `false`.
  This avoids starting the continuous `ReadDevice()` loop by default. SMS polling remains enabled.
- Added configurable Gammu connection settings:
  - `gammu_connection`
  - `gammu_commtimeout`
  - `gammu_init_retries`
  - `gammu_init_retry_delay`
  - `gammu_reinit_after_failures`
  - `gammu_reinit_cooldown`
  - `gammu_operation_delay`
  - `callback_read_interval`
- Added startup diagnostics for `/dev/serial/by-id` and optional waiting for the modem path.
- Added legacy aliases compatible with the original gateway/component:
  - `GET /signal` -> `GET /status/signal`
  - `GET /network` -> `GET /status/network`
  - `GET /getsms` -> `GET /sms/getsms`
  - `GET /reset` -> full Gammu reinitialization
- `deleteSms()` now re-raises deletion errors, so failed deletes are no longer counted as successful Gammu operations.

## Recommended initial configuration

```yaml
device_path: "/dev/serial/by-id/your-modem-at-port"
gammu_connection: "at"
gammu_commtimeout: 30
gammu_init_retries: 3
gammu_reinit_after_failures: 3
gammu_reinit_cooldown: 60
sms_monitoring_enabled: true
sms_check_interval: 120
missed_calls_monitoring_enabled: false
voice_call_enabled: false
auto_delete_read_sms: true
```

If the modem is unstable with `at`, try `at115200`.

For SIMCom/Quectel multi-interface modems, avoid GNSS/NMEA/diagnostic interfaces. Use the actual AT/SMS port.
