@rem SPDX-License-Identifier: AGPL-3.0-or-later
@rem Copyright (C) 2026 Akari Sora
@rem Windows shim for the FAKE baw of the fork rehearsal: Windows cannot start the extensionless Python
@rem script next to this file. run_fork.py puts this folder first on PATH and sets FAKEBAW_PYTHON to its
@rem own interpreter. Nothing may be echoed: callers parse stdout as JSON.
@"%FAKEBAW_PYTHON%" "%~dp0baw" %*
