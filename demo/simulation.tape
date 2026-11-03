# Fork simulation: the real PearlMiningV2 on a local anvil fork of BSC, a fake baw that only talks to 127.0.0.1,
# a live opening and a competitor that eats the daily cap. Nothing is sent to the real chain.
# Needs anvil on PATH, PEARL_WALLET / PEARL_HANDLE in the environment and submit/proofs.json (your own).
# --anonymize keeps your address, tx hashes, clock times and temp paths off the recording.
# Render from the repository root:  vhs demo/simulation.tape
# Then shrink the GIF (16 colours, still sharp for terminal text):
#   ffmpeg -i demo/simulation.gif -vf "fps=8,split[a][b];[a]palettegen=max_colors=16:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" small.gif && mv small.gif demo/simulation.gif
Output demo/simulation.gif
Output demo/simulation.mp4

Set Shell "bash"
Set FontFamily "Menlo"
Set FontSize 14
Set Width 1440
Set Height 900
Set Padding 26
Set Theme "Catppuccin Mocha"
Set WindowBar Colorful
Set Framerate 10
Set PlaybackSpeed 2.5
Set TypingSpeed 30ms

Hide
Type "export PATH=$HOME/.foundry/bin:$PATH && clear"
Enter
Sleep 500ms
Show

Type "# anvil forks BSC mainnet; fake baw talks to 127.0.0.1 only; the opening happens live 30 s from now"
Enter
Sleep 800ms
Type "python3 submit/fork_rehearsal/run_fork.py --anonymize --cap-proofs 400 --competitor-rate 4 --minutes 3"
Sleep 600ms
Enter
# vhs `Wait+Screen` only scans the first screenful of the buffer, so wait for the ~2 min run with a fixed sleep
Sleep 165s
