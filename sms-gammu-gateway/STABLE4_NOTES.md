# Stable4 notes

Stable4 builds on stable3 and targets a modem state observed on Huawei/USB
modems: after an incoming voice call attempt, the modem can keep answering
basic status commands while mobile-terminated SMS delivery remains delayed until
an outgoing operation wakes the modem/SMS stack.

Changes:

- Added periodic SMS reception recovery.
  - `sms_recovery_mode`: `off`, `reinit`, or `soft_reset`.
  - `sms_recovery_interval`: seconds between proactive recovery attempts.
  - default: `reinit` every 300 seconds.
- Added manual recovery endpoint: `GET /status/sms-recover`.
- Added post-send SMS polling burst.
  - After a successful outgoing SMS, the monitor polls immediately and then more
    frequently for `sms_burst_after_send_seconds`.
- SMS monitoring no longer depends only on an increasing message count.
  - Every cycle scans for unseen `Read`/`UnRead` SMS entries.
  - This avoids missing messages when storage count is distorted by saved/sent
    messages or reused slots.

Recommended starting settings:

```yaml
sms_monitoring_enabled: true
sms_check_interval: 60
auto_delete_read_sms: true
missed_calls_monitoring_enabled: false
voice_call_enabled: false

gammu_process_worker: true
gammu_operation_timeout: 90
sms_recovery_mode: "reinit"
sms_recovery_interval: 300
sms_burst_after_send_seconds: 120
sms_burst_interval: 10
```

If SMS still remains delayed after incoming call attempts, try the stronger mode:

```yaml
sms_recovery_mode: "soft_reset"
sms_recovery_interval: 300
sms_recovery_delay: 5
```

`soft_reset` calls `Gammu Reset(False)` and then reinitializes the isolated
worker. It is more disruptive than `reinit`, so it is not the first-choice
setting.
