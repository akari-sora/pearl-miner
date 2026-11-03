# Platforms: macOS, Linux, Windows and GPU vendors

[繁體中文](PLATFORMS.zh-TW.md)

pearl-miner is plain Python plus one OpenCL kernel, so it runs on macOS, Linux and Windows, and on any GPU with an
OpenCL 1.2 (or later) driver. This page says what has been tested where, how to set up each system, which commands
differ between them, and what to do when something does not work.

## Support matrix

"Tested" means run on that machine for this page; "expected" means it should work, but nobody has run it there yet.

### Components by operating system

| Component | macOS (Apple Silicon) | Linux x86_64 | Windows 11 x64 |
|---|---|---|---|
| Miner `miner/miner.py` | tested (M4 Max) | expected, untested: the test machine has no GPU OpenCL runtime, and pocl could not build any kernel there (see below) | tested (NVIDIA RTX 4070 SUPER, AMD Radeon iGPU) |
| Proof export `submit/export_proofs.py` | tested (manual run) | tested (manual run) | tested (manual run) |
| Submitter `submit/pearl_submit.py` | tested (offline suite, fork simulation) | tested (offline suite) | tested (offline suite) |
| Seller `submit/pearl_sell.py` | tested (offline suite) | tested (offline suite) | tested (offline suite) |
| Fork simulation `submit/fork_rehearsal/run_fork.py` | tested: `FORK REHEARSAL: PASS` | expected, untested | expected, untested |
| Log replay `tools/replay_log.py` | redaction tested (offline suite); replay itself not run | redaction tested (offline suite) | redaction tested (offline suite) |

"Manual run": `export_proofs.py` re-verified three proofs mined on the Mac, on each system; no automated test covers
it. The offline suites (`submit/test_submit.py`, `submit/test_sell.py`, `tools/test_replay_log.py`,
`miner/test_autotune.py`) start real processes,
take the real journal lock and run a fake `baw` the way the real one is run (through `baw.cmd` on Windows). The real
`baw` CLI and real transactions were not part of these tests on any system.

Test machines: macOS 27 on an Apple M4 Max (Python 3.14); Ubuntu 24.04 with Linux 7.0 on a Ryzen 7 9700X
(Python 3.12); Windows 11 on a Ryzen 9 9950X3D with an RTX 4070 SUPER (Python 3.11, NVIDIA driver 616.92).

### GPUs

| GPU | Status | `--test` | Speed (`--bench`) |
|---|---|---|---|
| Apple M4 Max (40-core GPU, unified memory), macOS built-in OpenCL 1.2 | tested | OK | about 700 MH/s ¹ |
| NVIDIA GeForce RTX 4070 SUPER, Windows 11, driver 616.92 | tested | OK | about 2.0–2.1 GH/s |
| AMD Radeon iGPU of the Ryzen 9 9950X3D (gfx1036, the small display GPU in the CPU, shares system memory), Windows 11, AMD Adrenalin driver | tested | OK | 31 MH/s |
| NVIDIA on Linux (proprietary driver) | expected, untested | | |
| AMD on Linux (ROCm, or Mesa rusticl) | expected, untested | | |
| AMD Ryzen AI APUs on Windows (e.g. HX 370, unified memory) | expected, untested | | |
| Intel Arc / Iris Xe (Windows, Linux) | expected, untested | | |
| CPU OpenCL runtimes (pocl, Intel CPU runtime) | untested; work only with `--device`, and slowly. Tried on Linux with the pip pocl 3.0 wheel (`pocl-binary-distribution`) on a Ryzen 7 9700X: pocl lists the CPU but cannot build any kernel there, not even a one-line one (`unknown target CPU 'generic'`), so the miner's kernel could not be checked with it | | |

¹ The Mac's GPU also drove the desktop during the tests, and the rate moved between about 300 and 700 MH/s with
the other load (up to 723 MH/s), for this version and the previous one alike. On the RTX 4070 SUPER this version and the previous one
measured the same, about 2.1 GH/s, in alternating 8-second runs.

What the kernel needs: OpenCL C 1.2 with 64-bit integers (`ulong`), the `rotate` builtin and a 32-bit global
`atomic_inc`. All of these are core OpenCL 1.2, so no vendor extension is used. GPUs with unified memory (Apple
M-series, AMD APUs, Intel iGPUs) need nothing special: the miner moves only a few kilobytes between host and GPU.

Apple GPUs are built for 32-bit arithmetic while Keccak works on 64-bit words, which is one reason a Mac mines more
slowly than a discrete NVIDIA or AMD card.

### Miner options that matter on other hardware

```sh
python3 miner/miner.py --list-devices          # every OpenCL device, numbered; the default is marked
python3 miner/miner.py --device 1 --test ...   # use device 1
python3 miner/miner.py --launch-ms 100 ...     # shorter kernel launches (default 250 ms)
```

- The default device is the first NVIDIA GPU, else the first GPU of any vendor. A machine with an integrated and a
  discrete GPU lists both: pick the fast one with `--device`.
- Each kernel launch is sized automatically to about `--launch-ms` (default 250 ms). A GPU that also drives a display
  must finish every launch quickly, or the operating system resets it (Windows: "TDR" after 2 s; macOS has a similar
  watchdog). `--iters N` fixes the work per launch instead, and `--global-size` sets the number of work-items.
- `--test` mines with an easy target, re-checks 2000 GPU hits on the CPU and compares the number of hits the GPU
  counted with the number expected; it prints the speed. Run it once on every new machine. It ends after a few short
  launches, before the launch size has grown to `--launch-ms`, so its speed is lower than `--bench`.

## Setup

All systems need Python 3.9 or later (3.11 or later on Windows; see below). Mining also needs numpy, pyopencl and
pycryptodome; the submitter and the seller need only the Python standard library and `baw`.

`baw` is the Binance Agentic Wallet CLI, an npm package that needs Node.js:
`npm install -g @binance/agentic-wallet`, then sign in and turn Developer Mode on as described in
[binance/binance-skills-hub](https://github.com/binance/binance-skills-hub). The fork simulation needs `anvil`
from [Foundry](https://getfoundry.sh).

### macOS

```sh
brew install python node                        # or the python.org installer
python3 -m venv .venv && . .venv/bin/activate
pip install -r miner/requirements.txt
npm install -g @binance/agentic-wallet          # baw
curl -L https://foundry.paradigm.xyz | bash   # anvil, for the fork simulation; then open a new terminal
foundryup                                      # (the installer adds ~/.foundry/bin to PATH for new shells only)
python3 miner/miner.py --list-devices
```

OpenCL is part of macOS (Apple deprecated it in 2018 but it is still present on Apple Silicon and Intel Macs);
there is no driver to install. With the python.org installer, run "Install Certificates.command" in the Python
folder once, or every RPC call fails with `CERTIFICATE_VERIFY_FAILED`.

### Linux

```sh
sudo apt install python3-venv clinfo           # Debian / Ubuntu names
python3 -m venv .venv && . .venv/bin/activate
pip install -r miner/requirements.txt
clinfo -l                                      # must list your GPU
python3 miner/miner.py --list-devices
```

The pyopencl wheel brings its own OpenCL loader, which reads the drivers listed in `/etc/OpenCL/vendors/`. Install
the OpenCL part of your GPU driver:

- NVIDIA: the proprietary driver (the OpenCL library comes with it, on Ubuntu as `libnvidia-compute-<version>`).
- AMD: ROCm (`rocm-opencl-runtime`) on supported cards; your user must be in the `render` and `video` groups. On
  cards ROCm does not support, Mesa's rusticl (`mesa-opencl-icd`, then `export RUSTICL_ENABLE=radeonsi`) may work.
- Intel: `intel-opencl-icd`.

Node.js for `baw`: your distribution's package, or [nodejs.org](https://nodejs.org). `anvil`: the Foundry installer
and then, in a new terminal, `foundryup`, as on macOS.

### Windows 10 / 11 (PowerShell)

```powershell
winget install Python.Python.3.12
winget install OpenJS.NodeJS.LTS
py -m venv .venv
.venv\Scripts\Activate.ps1        # if refused: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r miner\requirements.txt
npm install -g @binance/agentic-wallet          # installs baw.cmd in %APPDATA%\npm
python miner\miner.py --list-devices
```

- OpenCL comes with the normal GPU driver (NVIDIA Game Ready / Studio, AMD Adrenalin, Intel Arc / Iris); nothing
  else is needed. "Microsoft Basic Display Adapter" means no driver is installed.
- Use Python 3.11 or later: before 3.11, `time.sleep` on Windows can oversleep by up to about 15 ms; from 3.11 on it
  uses a high-resolution timer.
- npm installs `baw` as `baw.cmd`. The tools look it up on `PATH` themselves; open a new terminal after installing.
  Windows runs a `.cmd` through cmd.exe, so the tools refuse a `baw.cmd` whose folder contains `%`, `!` or `"`
  (or, in a folder name without spaces, `& | < > ^ ( ) ; , =`). If yours does, move npm's global folder:
  `npm config set prefix C:\npm-global`, add that folder to PATH, and install `baw` again.
- Keep the clock in sync, because the submitter times its first preview against the chain: run `w32tm /resync` (as
  administrator) before the opening and look at the `local clock - block ts` line of `check`.
- Optional: `$env:PYTHONUTF8 = "1"` makes Python use UTF-8 everywhere. The tools already write their logs and
  journals as UTF-8 without it.
- `anvil` (fork simulation, not tested on Windows): run `foundryup` from Git Bash, or run the simulation under WSL.
- Turn off sleep for the time of a run (Settings, System, Power), and do not let Windows Update restart the machine.

## Commands that differ

Everything else in the README works as written on all three systems: use `python` (or `py`) instead of `python3`
on Windows, and `\` in paths if you prefer.

### Wallet and handle

```sh
# macOS / Linux
export PEARL_WALLET=0xYourAgenticWalletAddress
export PEARL_HANDLE=yourhandle
```

```powershell
# Windows PowerShell (this window only; cmd.exe: set PEARL_WALLET=0x...)
$env:PEARL_WALLET = "0xYourAgenticWalletAddress"
$env:PEARL_HANDLE = "yourhandle"
```

### Run the submitter in the background, with run.pid

The seller refuses to sell while the process in `submit/run.pid` is alive, and also when that file is missing, so
always write it.

```sh
# macOS / Linux
nohup python3 submit/pearl_submit.py run > submit/run.log 2>&1 & echo $! > submit/run.pid
tail -f submit/run.log
```

```powershell
# Windows PowerShell, from the repository folder
$p = Start-Process -FilePath python -ArgumentList "submit\pearl_submit.py", "run" `
       -RedirectStandardOutput submit\run.log -RedirectStandardError submit\run.err `
       -WindowStyle Hidden -PassThru
$p.Id | Set-Content -Encoding ascii submit\run.pid
Get-Content submit\run.log -Wait -Encoding UTF8          # follow the log; Ctrl-C stops only the viewing
```

- `Start-Process` needs two different files for output and errors; read `submit\run.err` as well if something
  looks wrong.
- The variables set with `$env:` in the same window are passed on to the new process.
- `-WindowStyle Hidden` keeps the run going when you close PowerShell. With `-NoNewWindow` it would share your window
  and stop when the window closes.
- Inside a virtual environment, `python` is a small launcher that starts the real interpreter and waits for it.
  `run.pid` then holds the launcher's id, which stays alive exactly as long as the run, so the seller's check still
  works.

### Stop

```sh
# macOS / Linux
touch submit/STOP        # the miner: touch miner/STOP
rm submit/STOP           # before the next start
```

```powershell
# Windows PowerShell
New-Item -ItemType File submit\STOP        # the miner: New-Item -ItemType File miner\STOP
Remove-Item submit\STOP                    # before the next start
```

The STOP file is the way to stop on every system: the run finishes the step it is in, writes its journal and its
summary, and exits. For the submitter, Ctrl-C in its own terminal does the same. On Windows, do not use `Stop-Process` or Task
Manager: they end the whole run at once (in a virtual environment the launcher takes the real interpreter down with
it), with no summary, and a `baw` call that was in progress may still complete after the run is gone. Its journal is
safe (every record is on disk before the next step), but you lose the stop record and the summary.

### Mine in the background

```sh
# macOS / Linux
nohup python3 miner/miner.py --out miner/proofs.csv > miner/miner.log 2>&1 &
```

```powershell
# Windows PowerShell
Start-Process -FilePath python -ArgumentList "miner\miner.py", "--out", "miner\proofs.csv" `
  -RedirectStandardOutput miner\miner.log -RedirectStandardError miner\miner.err -WindowStyle Hidden
```

## Troubleshooting

**`no OpenCL platform found`, or `--list-devices` shows no GPU**

- Windows: install or reinstall the GPU vendor's driver (not the one Windows Update picks for a new card). Remote
  Desktop sessions can hide the GPU on some drivers; try at the console.
- Linux: `clinfo -l` must list the GPU. If it does not, the driver's OpenCL part is missing (see Setup) or
  `/etc/OpenCL/vendors/` is empty; for AMD check the `render` / `video` groups and log in again.
- macOS: OpenCL is built in; make sure you run the Python of the virtual environment where pyopencl is installed.
- A computer with several GPUs: `--list-devices`, then `--device N`.

**The kernel does not build (`OpenCL kernel build failed`)**

The message includes the compiler's log. Update the GPU driver first. If the log says `unknown target CPU 'generic'`
on a pocl CPU device, that pocl build does not know your CPU and builds no kernel at all; it is not this kernel. Then try another device with `--device`
(a CPU OpenCL device can work too, slowly), and report the log with the device name and driver version from
`--list-devices`.

**The screen freezes or flickers, "Display driver stopped responding and has recovered" (Windows), errors such as
`OUT_OF_RESOURCES` or `-36`, or a stuck desktop on macOS**

The GPU watchdog reset a kernel launch that took too long. Use shorter launches, e.g. `--launch-ms 100`, or leave the
display on another GPU. Do not raise Windows' `TdrDelay` registry value unless you know what it does.

**`CompilerWarning: Non-empty compiler output encountered`** when the miner starts

Information only: the driver's compiler printed notes. `--test` tells you whether the kernel works.

**`baw not found on PATH`**

- `which baw` (macOS / Linux) or `where.exe baw` (Windows) must print a path.
- `npm prefix -g` shows where npm puts global CLIs; on Windows that folder (usually `%APPDATA%\npm`) must be on PATH.
- Open a new terminal after `npm install -g`.

**Garbled 珍珠币 in a log on Windows**

The logs are UTF-8. Read them with `Get-Content -Encoding UTF8`, or in an editor that reads UTF-8 (Notepad does).

**`CERTIFICATE_VERIFY_FAILED` (macOS, python.org installer)**

Run "Install Certificates.command" from the Python folder in Applications.

**`another run already holds ... journal.jsonl`**

A second submitter is running on the same journal: stop that one first. The lock is released when a run exits, even
if it crashed. It belongs to the journal file itself, so a second run is refused however it names the file, and
nothing is left behind.
