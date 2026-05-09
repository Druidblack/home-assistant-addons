# Stable5 notes

Stable5 keeps the isolated Gammu process worker from stable3/stable4 and adds raw AT recovery modes for Huawei/Hilink-style modems where incoming SMS can remain buffered after a voice-call/network event until the modem performs a deeper outgoing activity.

New `sms_recovery_mode` values:

- `at_wake`: terminate Gammu, open the serial port directly, run SMS init commands (`CMGF`, `CPMS`, `CNMI`), reinitialize Gammu, then poll SMS.
- `network_wake`: same as `at_wake`, but first cycles the radio with `AT+CFUN=0` / `AT+CFUN=1` to force network re-registration.

New options:

```yaml
sms_wake_at_timeout: 5
sms_wake_command_delay: 0.5
```

Suggested test order for Huawei modems:

1. `sms_recovery_mode: at_wake`
2. If delayed SMS still appear only after sending from the modem, try `sms_recovery_mode: network_wake`.

`network_wake` is more disruptive because it temporarily detaches and re-registers the modem on the mobile network.
