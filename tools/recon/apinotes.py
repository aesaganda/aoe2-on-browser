"""Shim-difficulty notes for every Win32 import of age2_x1.exe.

Difficulty is judged for the *final* target: a Linux-native build (Phase 1) and a
browser build through Emscripten (Phase 2).
  trivial - a few lines over libc/SDL, or a constant/no-op
  medium  - real code but well understood, e.g. a small state machine or a host
            API mapping with edge cases
  hard    - a subsystem on its own (COM object model, networking, video/MCI) or
            something the browser makes awkward (blocking waits, threads)
"""

T, M, H = "trivial", "medium", "hard"

NOTES: dict[str, tuple[str, str]] = {
    # ---------------- VERSION
    "GetFileVersionInfoA": (T, "Return the exe's own version resource (it is already in guest memory)"),
    "GetFileVersionInfoSizeA": (T, "Same as above"),
    "VerQueryValueA": (M, "Walk the VS_VERSIONINFO tree; small parser"),
    # ---------------- MSVFW32 (Video for Windows)
    "ICInfo": (T, "Codec enumeration; return FALSE (no codecs) or a fake entry"),
    "MCIWndCreateA": (H, "AVI intro/cinematic playback window. Plan: decode AVIs on the host (or a WebCodecs/<video> path in the browser), or skip the videos"),
    # ---------------- WINMM
    "timeBeginPeriod": (T, "No-op"),
    "timeEndPeriod": (T, "No-op"),
    "timeGetTime": (T, "Monotonic ms clock (clock_gettime / performance.now). Must be consistent for lockstep timing"),
    "mciSendCommandA": (H, "MCI: CD audio / MIDI / AVI depending on the device string. Look at which devices are opened before planning"),
    "mciGetErrorStringA": (T, "Static strings"),
    "mixerGetNumDevs": (T, "Return 0 or 1"),
    "mixerOpen": (M, "Fake one mixer; the game uses it for CD/wave volume"),
    "mixerClose": (T, "No-op"),
    "mixerGetLineInfoA": (M, "Describe a fake line"),
    "mixerGetLineControlsA": (M, "Describe a fake volume control"),
    "mixerGetControlDetailsA": (M, "Read volume from host state"),
    "mixerSetControlDetails": (M, "Write volume to host audio gain (no A/W variant: exported undecorated)"),
    # ---------------- KERNEL32: memory
    "GlobalAlloc": (T, "Guest heap allocation"), "GlobalFree": (T, "Guest heap free"),
    "GlobalLock": (T, "Return pointer (fixed memory)"), "GlobalUnlock": (T, "No-op"),
    "GlobalReAlloc": (T, "Guest heap realloc"), "GlobalHandle": (T, "Identity for fixed memory"),
    "GlobalMemoryStatus": (T, "Fill MEMORYSTATUS with plausible constants"),
    "HeapCreate": (T, "Guest heap"), "HeapDestroy": (T, "Guest heap"), "HeapAlloc": (T, "Guest heap"),
    "HeapFree": (T, "Guest heap"), "HeapReAlloc": (T, "Guest heap"), "HeapSize": (T, "Guest heap; allocator must track sizes"),
    "VirtualAlloc": (M, "Reserve/commit inside the 32-bit guest space; the CRT's small-block heap uses reserve-then-commit"),
    "VirtualFree": (M, "Pair of VirtualAlloc"),
    "IsBadReadPtr": (T, "Range check against mapped guest regions"),
    "IsBadWritePtr": (T, "Same"), "IsBadCodePtr": (T, "Same"),
    # ---------------- KERNEL32: files
    "CreateFileA": (M, "Path translation (\\ -> /, case-insensitive lookup); OPFS in the browser"),
    "ReadFile": (M, "Synchronous read; in the browser it needs OPFS sync access handles in a worker"),
    "WriteFile": (M, "As ReadFile"), "SetFilePointer": (T, "lseek"), "GetFileSize": (T, "fstat"),
    "SetEndOfFile": (T, "ftruncate"), "FlushFileBuffers": (T, "fsync / no-op"), "CloseHandle": (M, "Handle table shared by files, events, mutexes, mappings"),
    "GetFileType": (T, "FILE_TYPE_DISK / CHAR for std handles"), "GetFileAttributesA": (T, "stat + path translation"),
    "DeleteFileA": (T, "unlink"), "CreateDirectoryA": (T, "mkdir"),
    "FindFirstFileA": (M, "Wildcard dir scan, case-insensitive; used to list scenarios/saves/AI scripts"),
    "FindNextFileA": (M, "As above"), "FindClose": (T, "Free iterator"),
    "GetTempPathA": (T, "Constant path"), "GetTempFileNameA": (T, "mkstemp-like"),
    "GetFullPathNameA": (T, "String join"), "GetCurrentDirectoryA": (T, "Constant game root"),
    "SetCurrentDirectoryA": (T, "Track a virtual cwd"),
    "GetDiskFreeSpaceA": (T, "Return large constants"), "GetDriveTypeA": (T, "DRIVE_FIXED (CD checks already removed)"),
    "GetVolumeInformationA": (T, "Fake label/serial; check whether a CD check still calls it"),
    "OpenFile": (M, "Legacy OFSTRUCT API; map onto CreateFile"),
    "_lread": (T, "read"), "_hread": (T, "read"), "_llseek": (T, "lseek"), "_lclose": (T, "close"),
    "CreateFileMappingA": (M, "File mapping: copy the file into guest memory (read-only use is the common case)"),
    "MapViewOfFile": (M, "Pair of the above"), "UnmapViewOfFile": (T, "Free / write back"),
    # ---------------- KERNEL32: modules/resources
    "GetModuleHandleA": (T, "Return image base for NULL/self, fake handles for known DLLs"),
    "GetModuleFileNameA": (T, "Constant path"),
    "LoadLibraryA": (M, "Return fake handles for the DLLs the game probes; it must not load real code. List which names it asks for"),
    "GetProcAddress": (M, "Table of HLE functions keyed by name; unknown names -> NULL"),
    "FreeLibrary": (T, "No-op"),
    "FindResourceA": (M, "Walk the PE resource directory (already mapped)"),
    "LoadResource": (T, "Pointer into the image"), "LockResource": (T, "Identity"),
    # ---------------- KERNEL32: process/thread/sync
    "GetCurrentProcess": (T, "Pseudo-handle"), "GetCurrentThread": (T, "Pseudo-handle"),
    "SetPriorityClass": (T, "No-op"), "GetPriorityClass": (T, "Constant"),
    "SetThreadPriority": (T, "No-op"), "GetThreadPriority": (T, "Constant"),
    "CreateMutexA": (M, "Single-instance check plus locking; trivial if the game is single-threaded"),
    "ReleaseMutex": (M, "As above"),
    "CreateSemaphoreA": (M, "Needs real semantics only if there are other threads (none are created through the import table: check GetProcAddress)"),
    "ReleaseSemaphore": (M, "As above"),
    "WaitForSingleObject": (H, "Blocking wait. Fine natively; in the browser it needs a worker + Atomics.wait or Asyncify"),
    "ExitProcess": (T, "Unwind to host main loop"), "TerminateProcess": (T, "Same"),
    "WinExec": (T, "Launching another program (e.g. the help/website); no-op"),
    "GetCommandLineA": (T, "Constant string"), "GetStartupInfoA": (T, "Zero-filled struct"),
    "GetEnvironmentStrings": (T, "Small static block"), "GetEnvironmentStringsW": (T, "Same, UTF-16"),
    "FreeEnvironmentStringsA": (T, "No-op"), "FreeEnvironmentStringsW": (T, "No-op"),
    "SetEnvironmentVariableA": (T, "Store in a map"),
    "GetStdHandle": (T, "Fixed handles"), "SetStdHandle": (T, "Store"), "SetHandleCount": (T, "Return arg"),
    "GetVersion": (T, "Constant (claim Windows 98 or XP; pick what the game expects)"),
    "GetVersionExA": (T, "Same, struct form"),
    # ---------------- KERNEL32: errors / SEH
    "GetLastError": (T, "Per-thread variable"), "SetLastError": (T, "Same"),
    "RaiseException": (H, "Needs the SEH model: the recompiled code must be able to unwind to __except/catch handlers (MSVC C++ EH goes through here)"),
    "RtlUnwind": (H, "Part of the same SEH machinery used by _except_handler3 and __CxxFrameHandler"),
    "UnhandledExceptionFilter": (T, "Log and abort"), "SetUnhandledExceptionFilter": (T, "Store pointer"),
    "OutputDebugStringA": (T, "stderr"),
    # ---------------- KERNEL32: time
    "GetLocalTime": (T, "localtime"), "GetSystemTime": (T, "gmtime"), "FileTimeToSystemTime": (T, "Arithmetic"),
    "FileTimeToLocalFileTime": (T, "Arithmetic + tz"), "GetTimeZoneInformation": (T, "Fixed/UTC"),
    "GetDateFormatA": (M, "Locale formatting; a fixed en-US format is enough"),
    "GetTimeFormatA": (M, "Same"),
    # ---------------- KERNEL32: strings/locale
    "MultiByteToWideChar": (M, "Code page 1252 (and ACP) to UTF-16; the CRT also asks for 0/CP_ACP"),
    "WideCharToMultiByte": (M, "Reverse"),
    "GetACP": (T, "1252"), "GetOEMCP": (T, "437"), "GetCPInfo": (T, "Single-byte code page"),
    "IsDBCSLeadByte": (T, "FALSE for 1252 (localised East-Asian builds would need real tables)"),
    "CompareStringA": (M, "Locale-aware compare; ordinal with case folding is close enough"),
    "CompareStringW": (M, "Same"), "LCMapStringA": (M, "Upper/lower case mapping"), "LCMapStringW": (M, "Same"),
    "GetStringTypeA": (M, "Char classification tables"), "GetStringTypeW": (M, "Same"),
    "MulDiv": (T, "(a*b)/c with rounding; 64-bit intermediate"),
    # ---------------- USER32: windowing
    "RegisterClassA": (M, "Store WNDPROC; the window manager is a single top-level window"),
    "CreateWindowExA": (M, "One main window maps to the host window/canvas; child windows (edit boxes?) need checking"),
    "DestroyWindow": (T, "Bookkeeping"), "ShowWindow": (T, "No-op"), "UpdateWindow": (T, "Send WM_PAINT"),
    "MoveWindow": (T, "Bookkeeping"), "SetWindowPos": (T, "Bookkeeping"),
    "GetWindowRect": (T, "Fixed rect"), "GetClientRect": (T, "Fixed rect"),
    "ClientToScreen": (T, "Identity offset"), "ScreenToClient": (T, "Identity offset"),
    "SetWindowLongA": (M, "Subclassing (GWL_WNDPROC) must be honoured"), "GetWindowLongA": (M, "Same"),
    "SetClassLongA": (T, "Store"), "CallWindowProcA": (M, "Call guest WNDPROC through the dispatcher"),
    "DefWindowProcA": (M, "Default handling for the handful of messages the game forwards"),
    "SetWindowTextA": (T, "Title"), "GetWindowTextA": (T, "Title"),
    "GetParent": (T, "Bookkeeping"), "IsWindow": (T, "Bookkeeping"), "IsIconic": (T, "FALSE"),
    "FindWindowA": (T, "Single-instance check: return NULL"),
    "GetForegroundWindow": (T, "Main window"), "SetForegroundWindow": (T, "No-op"), "GetActiveWindow": (T, "Main window"),
    "BringWindowToTop": (T, "No-op"), "GetLastActivePopup": (T, "Main window"),
    "GetWindowThreadProcessId": (T, "Constants"),
    "InvalidateRect": (T, "Queue WM_PAINT"), "ValidateRect": (T, "No-op"), "GetUpdateRect": (T, "Empty rect"),
    "GetSystemMetrics": (T, "Constants for the chosen resolution"),
    "SystemParametersInfoA": (T, "Mostly SPI_SCREENSAVERRUNNING/mouse settings; constants"),
    "SetSysColors": (T, "No-op"), "GetSysColor": (T, "Constant table"),
    "LoadIconA": (T, "Fake handle"), "LoadCursorA": (T, "Fake handle"), "SetCursor": (T, "The game draws its own cursor"),
    "LoadStringA": (M, "String table lookup in .rsrc (or language.dll, loaded with LoadLibrary)"),
    "MessageBoxA": (T, "Log; return IDOK"), "MessageBeep": (T, "No-op"), "WinHelpA": (T, "No-op"),
    "wsprintfA": (M, "printf-style formatting with Win32 quirks; reimplement in C"),
    "CharUpperA": (T, "Code page 1252 upper-case"),
    "SetRect": (T, "Fill RECT"), "FillRect": (M, "GDI fill on a DC (see GDI)"),
    "DrawTextA": (M, "GDI text; see GDI note"), "DrawTextExA": (M, "Same"),
    "GetDC": (M, "DC for the window or a DirectDraw surface (GetDC on surfaces is a DirectDraw method)"),
    "ReleaseDC": (T, "Pair"),
    # ---------------- USER32: messages/input
    "GetMessageA": (H, "Blocking message loop. In the browser the loop has to give control back each frame (Asyncify or restructure); natively an SDL event pump"),
    "PeekMessageA": (M, "Translate host events (SDL/DOM) into WM_* messages"),
    "TranslateMessage": (T, "WM_KEYDOWN -> WM_CHAR"), "DispatchMessageA": (M, "Call guest WNDPROC"),
    "PostMessageA": (M, "Message queue"), "SendMessageA": (M, "Synchronous WNDPROC call; used for child controls too"),
    "PostQuitMessage": (T, "WM_QUIT"),
    "SetTimer": (M, "WM_TIMER generation from the pump"), "KillTimer": (T, "Remove"),
    "GetKeyState": (T, "Host key table"), "GetAsyncKeyState": (T, "Same"), "GetKeyboardState": (T, "Same"),
    "MapVirtualKeyA": (T, "Static table"), "GetKeyNameTextA": (T, "Static table (hotkey UI)"),
    "GetCursorPos": (T, "Host mouse"), "SetCursorPos": (M, "Pointer lock in the browser; edge-scrolling depends on it"),
    "SetCapture": (T, "Flag"), "ReleaseCapture": (T, "Flag"), "GetCapture": (T, "Flag"),
    "GetFocus": (T, "Bookkeeping"), "SetFocus": (T, "Bookkeeping"),
    "GetCaretBlinkTime": (T, "530"),
    "OpenClipboard": (T, "Host clipboard or a private one"), "CloseClipboard": (T, "Same"),
    "GetClipboardData": (M, "Text only"), "IsClipboardFormatAvailable": (T, "CF_TEXT"),
    # ---------------- GDI32
    "CreateFontIndirectA": (H, "Fonts. The game renders UI text through GDI (TextOut/DrawText on surface DCs). Need a rasteriser with matching metrics (bundle a bitmap font or FreeType + Arial/Times substitutes)"),
    "GetTextMetricsA": (M, "Part of the font work"), "GetTextExtentPoint32A": (M, "Part of the font work; layout depends on it"),
    "TextOutA": (H, "Text into an 8-bit surface DC: palette-index colours, transparency"),
    "SetTextColor": (T, "DC state"), "SetBkColor": (T, "DC state"), "SetBkMode": (T, "DC state"), "SetTextAlign": (T, "DC state"),
    "SelectObject": (T, "DC state"), "DeleteObject": (T, "Handle table"), "GetObjectA": (T, "Handle table"),
    "GetStockObject": (T, "Constants"), "CreatePen": (T, "Handle"), "MoveToEx": (M, "Line drawing into the DC's bitmap"), "LineTo": (M, "Same"),
    "CreateRectRgn": (T, "Rect region"), "SelectClipRgn": (M, "Clip rect for later GDI ops"),
    "DeleteDC": (T, "Handle"), "CreateICA": (T, "Information context: device caps only"),
    "GetDeviceCaps": (T, "Constants (BITSPIXEL=8, RASTERCAPS with RC_PALETTE ...)"),
    "CreatePalette": (M, "GDI palette objects, shared with DirectDraw's palette"),
    "SelectPalette": (T, "DC state"), "RealizePalette": (T, "No-op / push palette to host"),
    "GetPaletteEntries": (T, "Copy"), "SetPaletteEntries": (T, "Copy"), "ResizePalette": (T, "Realloc"),
    "GetNearestPaletteIndex": (M, "Colour match (simple distance search)"),
    "GetSystemPaletteEntries": (T, "Copy the current palette"),
    # ---------------- DirectX entry points (COM; the work is in the interfaces)
    "DirectDrawCreate": (H, "IDirectDraw + surfaces/palettes/clippers. 8-bit palettised, CPU-drawn: the presentation step is a palette lookup + canvas blit. Surface Lock/Unlock/Blt/BltFast/Flip semantics have to be exact"),
    "DSOUND.dll#1": (H, "DirectSoundCreate: IDirectSound + buffers (Lock/Unlock/Play/SetVolume/SetPan/GetCurrentPosition). Map to SDL audio / WebAudio AudioWorklet"),
    "DPLAYX.dll#1": (H, "DirectPlayCreate: lockstep multiplayer transport. Phase 3 replaces it with WebRTC/WebSocket; Phase 1 can return DPERR_UNAVAILABLE"),
    "DPLAYX.dll#2": (M, "DirectPlayEnumerateA: enumerate service providers via a guest callback"),
    "DPLAYX.dll#4": (H, "DirectPlayLobbyCreateA: lobby launch path (MSN Gaming Zone). Phase 1 can return an error"),
    # ---------------- IMM32 (IME)
    "ImmAssociateContext": (T, "No-op"), "ImmGetContext": (T, "NULL"), "ImmGetOpenStatus": (T, "FALSE"),
    "ImmSetOpenStatus": (T, "No-op"), "ImmNotifyIME": (T, "No-op"), "ImmGetDefaultIMEWnd": (T, "NULL"),
    "ImmReleaseContext": (T, "No-op"),
    # ---------------- ADVAPI32 (registry)
    "RegCreateKeyExA": (M, "Small registry backed by a JSON/INI file (install path, CD path, player settings)"),
    "RegQueryValueExA": (M, "Same"), "RegSetValueExA": (M, "Same"), "RegCloseKey": (T, "Same"),
    # ---------------- OLE32
    "CoInitialize": (T, "No-op"), "CoUninitialize": (T, "No-op"),
    "CoCreateInstance": (H, "Class factory for the CLSIDs the game asks for (see the COM section): returns the HLE DirectPlay/DirectPlayLobby objects"),
    "CoCreateGuid": (T, "Random 16 bytes (session/player GUIDs; not used by sim determinism)"),
    # ---------------- WSOCK32
    "WSAStartup": (T, "Succeed"), "WSACleanup": (T, "No-op"),
    "gethostname": (T, "Constant"), "gethostbyname": (M, "Local address for the 'your IP' display; constant 127.0.0.1 or the WebRTC address"),
}


def note(dll: str, name: str) -> tuple[str, str]:
    if name in NOTES:
        return NOTES[name]
    key = f"{dll}#{name.lstrip('#')}" if name.startswith("#") else name
    return NOTES.get(key, ("?", "not classified yet"))
