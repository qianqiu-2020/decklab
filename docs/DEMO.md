# Try DeckLab without a board

From a checkout, run:

```sh
python3 seriald_demo.py
```

Open **http://127.0.0.1:8899/**. Linux needs Python 3.10+ only. Windows source
runs also need the normal Windows Python dependencies; packaged Windows builds
include them and can run `seriald.exe demo`. On Ubuntu packages, use
`python3 /usr/lib/seriald/seriald_demo.py`.
Use `--port 8900` to choose another port. Stop with Ctrl+C.

The demo always binds loopback and creates a new temporary data directory.
It ignores production configuration and removes its data on a clean exit.
No collector, serial adapter, real SSH server or privileged helper is required.
Board files exist only in memory; uploaded/downloaded paths do not address host
files. Unsupported commands show help and are never executed on the host.

## Explore the workflow

The simulated serial and SSH terminals echo keystrokes before Enter, handle
Backspace/Delete and Ctrl-C, and execute only the limited commands listed below.
Choose 中文 / English in the interface without reconnecting terminals.

- Select `demo-k1` or `demo-k3`; `demo-offline` illustrates historical devices.
- Open serial or SSH and enter `uname -a`, `pwd`, `echo hello`, `ls`,
  `cat /root/log.txt`, or `dmesg`. Enter `reboot` in serial to add a boot.
- Browse two archived boots, including one crossing midnight. Compare boards:
  the K3 demo's newer boot contains an `mmc0: timeout` difference.
- Download `/root/log.txt`, upload a local file to `/tmp/example.txt`, or copy
  between boards. Transfers use the application's normal chunking, compression,
  offset and SHA-256 checks against an in-memory board protocol adapter.
- Open the same terminal in two browser tabs to observe shared output.

Agents can use the same client and skill workflow as a real host:

```sh
python3 serialctl.py --url http://127.0.0.1:8899 devices
python3 serialctl.py --url http://127.0.0.1:8899 --device demo-k1 exec --command 'uname -a'
python3 serialctl.py --url http://127.0.0.1:8899 --device demo-k1 logs
python3 serialctl.py --url http://127.0.0.1:8899 --device demo-k1 boot-log --latest
```

Demo timing is synthetic: it demonstrates interfaces and correctness, not UART
throughput, USB recovery or real hardware compatibility. Simulated commands are
intentionally limited; Linux shell scripting and package installation are not
emulated. Password provisioning and hardware recovery are unavailable.
