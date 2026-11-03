# The offline test suites: a fake chain + fake baw for the submitter, a pool model + fake baw for the seller,
# and the log redaction rules.
# Render from the repository root:  vhs demo/tests.tape
Output demo/tests.gif
Output demo/tests.mp4

Set Shell "bash"
Set FontFamily "Menlo"
Set FontSize 13
Set Width 1440
Set Height 900
Set Padding 26
Set Theme "Catppuccin Mocha"
Set WindowBar Colorful
Set Framerate 10
Set PlaybackSpeed 8
Set TypingSpeed 25ms

Hide
Type "clear"
Enter
Sleep 300ms
Show

Type `python3 submit/test_submit.py | grep --line-buffered -E '^\[|^FAILED' | cut -c1-150; a=${PIPESTATUS[0]}; python3 submit/test_sell.py | grep -E '\[(PASS|FAIL)\]|^FAILED' | tail -n 9; b=${PIPESTATUS[0]}; python3 tools/test_replay_log.py | tail -n 4; echo "== exit codes: test_submit.py $a, test_sell.py $b, test_replay_log.py ${PIPESTATUS[0]}"`
Sleep 500ms
Enter
# vhs `Wait+Screen` only scans the first screenful of the buffer, so wait for both suites (~3-4 min) with a fixed sleep
Sleep 260s
