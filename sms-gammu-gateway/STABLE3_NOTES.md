# stable3

This build isolates python-gammu calls in a separate child process. The main Flask/MQTT
process no longer blocks forever if libgammu hangs during SendSMS or another serial call.

New options:

- `gammu_process_worker`: default `true`; owns the modem in a child process.
- `gammu_operation_timeout`: default `90`; hard timeout for any Gammu operation. On timeout
  the child process is killed and restarted.

Callback/ReadDevice monitoring is disabled when process worker is enabled. SMS receive uses
polling only, which is safer for unstable USB modem serial ports.
