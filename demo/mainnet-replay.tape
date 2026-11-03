# Replay of the real day-1 BSC mainnet run, from its log anonymized with tools/replay_log.py --anonymize
# (no hashes, no clock times, no block numbers, rounded amounts).
# Render from the repository root:  vhs demo/mainnet-replay.tape
Output demo/mainnet-replay.gif
Output demo/mainnet-replay.mp4

Set Shell "bash"
Set FontFamily "Menlo"
Set FontSize 15
Set Width 1440
Set Height 980
Set Padding 26
Set Theme "Catppuccin Mocha"
Set WindowBar Colorful
Set Framerate 10
Set TypingSpeed 35ms

Hide
Type `clear && printf '\033[1;33m%s\033[0m\n\033[2m%s\033[0m\n\n' 'Real BSC mainnet run, day 1 (2026-11-03), replayed from the log; wallet and tx hashes redacted' 'Original timing, shown as seconds since the opening (T); hashes masked, block numbers removed, BNB amounts rounded; the wait before the opening is skipped.'`
Enter
Sleep 500ms
Show

Type "python3 tools/replay_log.py demo/day1-mainnet.log --speed 1.0"
Sleep 600ms
Enter
# the whole replay fits on one screen (vhs Wait+Screen only scans the first screenful of the buffer)
Wait+Screen@240s /SUMMARY txs=/
Sleep 6s
