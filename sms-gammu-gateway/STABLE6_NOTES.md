# Stable6 notes

Stable6 optimizes `sms_recovery_mode: network_wake`.

Changes:

- Replaced the old fixed `sleep 8s` + `sleep 20s` radio-cycle waits with configurable timing.
- `AT+CFUN=0` now uses a short command timeout, because many Huawei modems emit URCs but no final `OK`.
- After `AT+CFUN=1`, the add-on polls `AT+CREG?` and continues as soon as the modem reports registered (`1` or `5`).
- Reduced default `sms_wake_command_delay` from `0.5` to `0.2`.

New options:

```yaml
sms_network_wake_cfun0_timeout: 2
sms_network_wake_off_delay: 2.0
sms_network_wake_register_timeout: 15
sms_network_wake_register_poll_interval: 1.0
sms_wake_command_delay: 0.2
```

For faster recovery on Huawei modems, start with:

```yaml
sms_recovery_mode: network_wake
sms_recovery_interval: 120
sms_network_wake_cfun0_timeout: 2
sms_network_wake_off_delay: 2
sms_network_wake_register_timeout: 15
sms_wake_command_delay: 0.1
```

If SMS delivery becomes unreliable, increase `sms_network_wake_register_timeout` to `20` or `25`.
