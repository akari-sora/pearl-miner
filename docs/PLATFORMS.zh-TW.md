# 各平台說明：macOS、Linux、Windows 與各家顯示卡

[English](PLATFORMS.md)

pearl-miner 只用 Python 加上一段 OpenCL 程式（在顯示卡上算雜湊的那一段），所以 macOS、Linux、Windows 都能跑；
顯示卡只要驅動程式支援 OpenCL 1.2 以上就行。這份文件說明：哪些組合真的測過、每個系統要裝什麼、哪幾個指令在
不同系統上寫法不一樣，以及出問題時怎麼處理。

## 支援一覽

「已測試」是指這份文件撰寫時真的在那台機器上跑過；「應該可用，未測試」是指照理可以，但還沒有人在那種環境跑過。

### 各元件在各系統上的狀態

| 元件 | macOS（Apple 晶片） | Linux x86_64 | Windows 11 x64 |
|---|---|---|---|
| 挖礦程式 `miner/miner.py` | 已測試（M4 Max） | 應該可用，未測試：測試機沒有顯示卡的 OpenCL 驅動，pocl 在那台也編譯不了任何 kernel（見下表） | 已測試（NVIDIA RTX 4070 SUPER、AMD Radeon 內顯） |
| 匯出證明 `submit/export_proofs.py` | 已測試（手動執行） | 已測試（手動執行） | 已測試（手動執行） |
| 送單程式 `submit/pearl_submit.py` | 已測試（離線測試、分叉模擬） | 已測試（離線測試） | 已測試（離線測試） |
| 賣幣程式 `submit/pearl_sell.py` | 已測試（離線測試） | 已測試（離線測試） | 已測試（離線測試） |
| 分叉模擬 `submit/fork_rehearsal/run_fork.py` | 已測試：`FORK REHEARSAL: PASS` | 應該可用，未測試 | 應該可用，未測試 |
| 紀錄重播 `tools/replay_log.py` | 打碼功能已測試（離線測試）；重播本身沒有跑 | 打碼功能已測試（離線測試） | 打碼功能已測試（離線測試） |

「手動執行」是指在每個系統上用 `export_proofs.py` 重新驗證了三個在 Mac 上挖到的證明，沒有自動測試涵蓋它。
離線測試（`submit/test_submit.py`、`submit/test_sell.py`、`tools/test_replay_log.py`、`miner/test_autotune.py`）
會真的啟動程式、真的上鎖，
也會用一個假的 `baw`，照真的 `baw` 的方式去呼叫它（Windows 上是透過 `baw.cmd`）。這次測試在任何系統上都**沒有**
用真的 `baw`，也沒有送出任何真的交易。

測試機：Apple M4 Max 上的 macOS 27（Python 3.14）；Ryzen 7 9700X 上的 Ubuntu 24.04、Linux 7.0（Python 3.12）；
Ryzen 9 9950X3D 加 RTX 4070 SUPER 的 Windows 11（Python 3.11，NVIDIA 驅動 616.92）。

### 顯示卡

| 顯示卡 | 狀態 | `--test` | 速度（`--bench`） |
|---|---|---|---|
| Apple M4 Max（40 核心 GPU，和系統共用記憶體），macOS 內建 OpenCL 1.2 | 已測試 | 通過 | 約 700 MH/s ¹ |
| NVIDIA GeForce RTX 4070 SUPER，Windows 11，驅動 616.92 | 已測試 | 通過 | 約 2.0–2.1 GH/s |
| Ryzen 9 9950X3D 的 AMD Radeon 內顯（gfx1036，CPU 裡附的小顯示晶片，和系統共用記憶體），Windows 11，AMD Adrenalin 驅動 | 已測試 | 通過 | 31 MH/s |
| Linux 上的 NVIDIA（官方驅動） | 應該可用，未測試 | | |
| Linux 上的 AMD（ROCm，或 Mesa 的 rusticl） | 應該可用，未測試 | | |
| Windows 上的 AMD Ryzen AI 處理器內顯（例如 HX 370，共用記憶體） | 應該可用，未測試 | | |
| Intel Arc / Iris Xe（Windows、Linux） | 應該可用，未測試 | | |
| 用 CPU 跑 OpenCL（pocl、Intel CPU runtime） | 未測試；要用 `--device` 指定，而且很慢。在 Linux 的 Ryzen 7 9700X 上試過 pip 版 pocl 3.0（`pocl-binary-distribution`）：pocl 看得到 CPU，但連一行的小程式都編譯不了（`unknown target CPU 'generic'`），所以沒辦法用它驗證挖礦的 kernel | | |

¹ 測試時這台 Mac 的 GPU 同時在顯示桌面，速度隨其他負載在約 300 到 723 MH/s 之間變動，新舊版本都一樣。
在 RTX 4070 SUPER 上新舊版本交替各跑 8 秒，速度相同，都約 2.1 GH/s。

這段顯示卡程式只用到 OpenCL 1.2 的基本功能（64 位元整數、`rotate`、32 位元的 `atomic_inc`），沒有用任何廠商
專屬的擴充功能。和系統共用記憶體的顯示卡（Apple M 系列、AMD APU、Intel 內顯）不需要特別設定：挖礦時電腦和顯示卡
之間只傳幾 KB 的資料。

Apple 的 GPU 主要為 32 位元運算設計，而 Keccak 雜湊是用 64 位元在算，這是 Mac 挖起來比 NVIDIA 或 AMD
獨立顯示卡慢的原因之一。

### 在不同硬體上會用到的挖礦選項

```sh
python3 miner/miner.py --list-devices          # 列出所有 OpenCL 裝置和編號，預設用的那個會標出來
python3 miner/miner.py --device 1 --test ...   # 改用 1 號裝置
python3 miner/miner.py --launch-ms 100 ...     # 每一次丟給顯示卡的工作縮短（預設 250 毫秒）
```

- 預設用第一張 NVIDIA 顯示卡；沒有 NVIDIA 就用第一張任何廠牌的顯示卡。同時有內顯和獨顯的電腦兩張都會列出來，
  用 `--device` 選快的那張。
- 程式會自動調整每一次丟給顯示卡的工作量，讓每次大約 `--launch-ms` 毫秒（預設 250）。如果這張顯示卡同時在接螢幕，
  每一次都必須很快做完，不然作業系統會以為顯示卡當掉而把它重設（Windows 叫「TDR」，超過 2 秒就重設；macOS 也有
  類似的保護）。想固定每次的工作量可以用 `--iters N`，`--global-size` 則是同時開多少條工作。
- `--test` 會用很容易的難度挖一下，把顯示卡找到的 2000 個結果拿到 CPU 上逐一重算，再比對顯示卡找到的數量是否
  符合預期，最後印出速度。每換一台電腦都先跑一次。它跑幾次很短的工作就結束了，工作量還沒調到 `--launch-ms`，
  所以印出的速度會比 `--bench` 低。

## 安裝

每個系統都要 Python 3.9 以上（Windows 建議 3.11 以上，原因見下面）。挖礦另外要 numpy、pyopencl、pycryptodome；
送單和賣幣程式只要 Python 內建的東西加上 `baw`。

`baw` 是幣安 Agentic Wallet 的命令列工具，用 npm 安裝，所以要先裝 Node.js：
`npm install -g @binance/agentic-wallet`，裝好之後照 [binance/binance-skills-hub](https://github.com/binance/binance-skills-hub)
的說明登入並打開 Developer Mode。分叉模擬另外要 [Foundry](https://getfoundry.sh) 的 `anvil`。

### macOS

```sh
brew install python node                        # 或用 python.org 的安裝程式
python3 -m venv .venv && . .venv/bin/activate
pip install -r miner/requirements.txt
npm install -g @binance/agentic-wallet          # 安裝 baw
curl -L https://foundry.paradigm.xyz | bash   # 安裝 anvil，分叉模擬用；裝完開一個新的終端機視窗
foundryup                                      # （安裝程式只會把 ~/.foundry/bin 加進「新開」的終端機的 PATH）
python3 miner/miner.py --list-devices
```

macOS 本身就有 OpenCL（Apple 在 2018 年宣布不再更新，但 Apple 晶片和 Intel 的 Mac 都還有），不用另外裝驅動。
如果 Python 是用 python.org 的安裝程式裝的，請到「應用程式」裡的 Python 資料夾，執行一次
「Install Certificates.command」，不然連區塊鏈節點時會出現 `CERTIFICATE_VERIFY_FAILED`。

### Linux

```sh
sudo apt install python3-venv clinfo           # Debian / Ubuntu 的套件名稱
python3 -m venv .venv && . .venv/bin/activate
pip install -r miner/requirements.txt
clinfo -l                                      # 這裡要看得到你的顯示卡
python3 miner/miner.py --list-devices
```

用 pip 裝的 pyopencl 自己帶了 OpenCL 載入器，它會去 `/etc/OpenCL/vendors/` 找驅動。請安裝顯示卡驅動裡的 OpenCL 部分：

- NVIDIA：官方驅動（OpenCL 已包含在內，Ubuntu 上是 `libnvidia-compute-<版本>`）。
- AMD：支援的顯示卡裝 ROCm（`rocm-opencl-runtime`），而且你的帳號要加入 `render` 和 `video` 群組。ROCm 不支援
  的顯示卡可以試 Mesa 的 rusticl（裝 `mesa-opencl-icd`，再 `export RUSTICL_ENABLE=radeonsi`）。
- Intel：`intel-opencl-icd`。

`baw` 需要的 Node.js 可以用發行版的套件，或從 [nodejs.org](https://nodejs.org) 下載。`anvil` 和 macOS 一樣：
先跑 Foundry 的安裝程式，再開一個新的終端機視窗執行 `foundryup`。

### Windows 10 / 11（PowerShell）

```powershell
winget install Python.Python.3.12
winget install OpenJS.NodeJS.LTS
py -m venv .venv
.venv\Scripts\Activate.ps1        # 如果被拒絕執行：Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r miner\requirements.txt
npm install -g @binance/agentic-wallet          # 會在 %APPDATA%\npm 裝一個 baw.cmd
python miner\miner.py --list-devices
```

- OpenCL 跟著顯示卡驅動一起裝好（NVIDIA Game Ready / Studio、AMD Adrenalin、Intel Arc / Iris），不用另外裝。
  如果裝置管理員裡顯示卡叫「Microsoft 基本顯示介面卡」，代表還沒裝驅動。
- 請用 Python 3.11 以上：3.11 之前的版本在 Windows 上 `time.sleep` 可能多睡十幾毫秒，3.11 起改用高精度計時器。
- npm 裝的 `baw` 實際上是 `baw.cmd`，程式會自己在 PATH 裡找到它；裝完記得開一個新的終端機視窗。
  Windows 會用 cmd.exe 來執行 `.cmd`，所以如果 `baw.cmd` 所在的資料夾名稱裡有 `%`、`!`、`"`（資料夾名稱沒有空白時，
  還包括 `& | < > ^ ( ) ; , =`），程式會拒絕執行。遇到這種情況，把 npm 的全域資料夾換個地方：
  `npm config set prefix C:\npm-global`，把那個資料夾加進 PATH，再重新安裝一次 `baw`。
- 電腦時間要準，因為送單程式是照區塊鏈的時間抓開盤的：開盤前用系統管理員身分執行 `w32tm /resync`，再看 `check`
  印出的 `local clock - block ts`（本機時間和區塊時間的差）。
- 可以加設 `$env:PYTHONUTF8 = "1"`，讓 Python 全面使用 UTF-8。不設也沒關係，程式寫的紀錄檔和日誌本來就是 UTF-8。
- `anvil`（分叉模擬，Windows 上沒有測試過）：在 Git Bash 裡執行 `foundryup`，或在 WSL 裡跑分叉模擬。
- 送單期間請關掉自動睡眠（設定 → 系統 → 電源），也不要讓 Windows Update 自動重開機。

## 不同系統寫法不一樣的指令

README 裡其他指令三個系統都一樣；只是 Windows 上把 `python3` 換成 `python`（或 `py`），路徑要用 `\` 也可以。

### 設定錢包和 handle

```sh
# macOS / Linux
export PEARL_WALLET=0xYourAgenticWalletAddress
export PEARL_HANDLE=yourhandle
```

```powershell
# Windows PowerShell（只在這個視窗有效；cmd.exe 的寫法是 set PEARL_WALLET=0x...）
$env:PEARL_WALLET = "0xYourAgenticWalletAddress"
$env:PEARL_HANDLE = "yourhandle"
```

### 在背景執行送單程式，並記下 run.pid

賣幣程式會檢查 `submit/run.pid` 裡記的那個程式是不是還在跑；還在跑就不賣，找不到這個檔案也不賣。所以一定要寫這個檔。

```sh
# macOS / Linux
nohup python3 submit/pearl_submit.py run > submit/run.log 2>&1 & echo $! > submit/run.pid
tail -f submit/run.log
```

```powershell
# Windows PowerShell，在專案資料夾裡執行
$p = Start-Process -FilePath python -ArgumentList "submit\pearl_submit.py", "run" `
       -RedirectStandardOutput submit\run.log -RedirectStandardError submit\run.err `
       -WindowStyle Hidden -PassThru
$p.Id | Set-Content -Encoding ascii submit\run.pid
Get-Content submit\run.log -Wait -Encoding UTF8          # 持續看紀錄；按 Ctrl-C 只會停止「看」，不會停止送單
```

- `Start-Process` 的一般輸出和錯誤輸出必須寫到兩個不同的檔案；覺得不對勁時，`submit\run.err` 也要看。
- 在同一個視窗用 `$env:` 設定的值，會自動傳給新開的程式。
- `-WindowStyle Hidden` 讓你關掉 PowerShell 視窗後程式還繼續跑。如果改用 `-NoNewWindow`，程式會跟你共用視窗，
  視窗一關它就跟著停。
- 在虛擬環境（venv）裡，`python` 其實是一個小啟動器，它會再開真正的 Python 並等它結束。所以 `run.pid` 記到的是
  啟動器的編號，而啟動器活著的時間剛好跟送單程式一樣長，賣幣程式的檢查一樣有效。

### 停止

```sh
# macOS / Linux
touch submit/STOP        # 挖礦程式用：touch miner/STOP
rm submit/STOP           # 下一次啟動前要刪掉
```

```powershell
# Windows PowerShell
New-Item -ItemType File submit\STOP        # 挖礦程式用：New-Item -ItemType File miner\STOP
Remove-Item submit\STOP                    # 下一次啟動前要刪掉
```

建立 STOP 檔是所有系統通用的停止方式：程式會把手上這一步做完、寫好日誌和總結，然後自己結束。對送單程式來說，
在它自己的視窗按 Ctrl-C 效果也一樣。Windows 上**不要**用 `Stop-Process` 或工作管理員去結束它：那會立刻把整個
送單程式結束掉（在虛擬環境裡，啟動器被砍時會連真正的程式一起帶走），沒有總結；而且當時正在進行的 `baw` 呼叫，
可能在程式結束後還是會完成。日誌本身不會壞（每一步之前都已經寫進磁碟），但會少了停止紀錄和總結。

### 在背景挖礦

```sh
# macOS / Linux
nohup python3 miner/miner.py --out miner/proofs.csv > miner/miner.log 2>&1 &
```

```powershell
# Windows PowerShell
Start-Process -FilePath python -ArgumentList "miner\miner.py", "--out", "miner\proofs.csv" `
  -RedirectStandardOutput miner\miner.log -RedirectStandardError miner\miner.err -WindowStyle Hidden
```

## 常見問題

**出現 `no OpenCL platform found`，或 `--list-devices` 看不到顯示卡**

- Windows：到顯示卡廠商的官網下載驅動重新安裝（不要只用 Windows Update 自動裝的版本）。有些驅動在遠端桌面連線時
  會看不到顯示卡，請改在電腦前直接操作試試。
- Linux：`clinfo -l` 要看得到顯示卡。看不到就是驅動的 OpenCL 部分沒裝（見上面「安裝」），或 `/etc/OpenCL/vendors/`
  是空的；AMD 還要確認帳號在 `render`、`video` 群組裡，加完群組要重新登入。
- macOS：OpenCL 是內建的；請確認你用的是裝了 pyopencl 的那個虛擬環境裡的 Python。
- 有好幾張顯示卡：先 `--list-devices`，再用 `--device N` 指定。

**顯示卡程式編譯失敗（`OpenCL kernel build failed`）**

錯誤訊息裡會附上編譯器的紀錄。先更新顯示卡驅動。如果是用 pocl 的 CPU 裝置，而紀錄寫著 `unknown target CPU 'generic'`，
代表那個 pocl 版本不認得你的 CPU，任何程式都編譯不了，不是這段 kernel 的問題。接著再用 `--device` 換一個裝置試試（CPU 的 OpenCL 裝置也可能可以，只是很慢）。
回報問題時請附上這段紀錄，以及 `--list-devices` 顯示的裝置名稱和驅動版本。

**螢幕凍結或閃一下、Windows 跳出「顯示器驅動程式已停止回應，並已成功復原」、出現 `OUT_OF_RESOURCES` 或 `-36`
之類的錯誤，或 Mac 畫面卡住**

這是作業系統的顯示卡保護機制把一次做太久的工作中斷了。把每次的工作縮短，例如加 `--launch-ms 100`；或讓螢幕接在
另一張顯示卡上。除非你很清楚後果，否則不要去改 Windows 登錄檔裡的 `TdrDelay`。

**挖礦程式一開始印出 `CompilerWarning: Non-empty compiler output encountered`**

只是提示：驅動的編譯器印了一些說明文字。程式能不能正常算，看 `--test` 的結果就知道。

**出現 `baw not found on PATH`**

- `which baw`（macOS / Linux）或 `where.exe baw`（Windows）要能印出一個路徑。
- `npm prefix -g` 會告訴你 npm 把指令裝在哪裡；Windows 上那個資料夾（通常是 `%APPDATA%\npm`）要在 PATH 裡。
- `npm install -g` 之後要開一個新的終端機視窗。

**Windows 上紀錄檔裡的「珍珠币」變成亂碼**

紀錄檔是 UTF-8 編碼。用 `Get-Content -Encoding UTF8` 看，或用支援 UTF-8 的編輯器開（記事本可以）。

**出現 `CERTIFICATE_VERIFY_FAILED`（macOS，用 python.org 安裝的 Python）**

到「應用程式」裡的 Python 資料夾，執行「Install Certificates.command」。

**出現 `another run already holds ... journal.jsonl`**

同一份日誌已經有另一個送單程式在用了，先把那一個停掉。程式結束時（就算是當掉）鎖就會自動解開。這個鎖是跟著
日誌檔案本身走的，所以不管第二個程式用什麼寫法指到同一個檔案都會被拒絕，也不會留下任何多餘的檔案。
