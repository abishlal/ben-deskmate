# Keyboard helper for "rephrase the selected text" (Ctrl+Alt+R). The pet shell starts it once and keeps it
# running, so each use is quick. It reads one command per line on standard input and answers with one JSON line:
#
#   grab        wait until the hotkey's keys are let go, note the active window, press Ctrl+C in it
#               -> {"ok":true,"hwnd":"<window>"}
#   paste <w>   press Ctrl+V, but only if window <w> is still the active one
#               -> {"ok":true} or {"ok":false,"error":"moved"}
#
# It only ever presses Ctrl+C and Ctrl+V. The clipboard itself is read and written by the shell.
$ErrorActionPreference = 'Stop'
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class PetKeys {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern short GetAsyncKeyState(int vKey);
    [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
    public static void Chord(byte key) {
        keybd_event(0x11, 0, 0, UIntPtr.Zero);       // Ctrl down
        keybd_event(key, 0, 0, UIntPtr.Zero);
        keybd_event(key, 0, 2, UIntPtr.Zero);        // KEYEVENTF_KEYUP
        keybd_event(0x11, 0, 2, UIntPtr.Zero);
    }
}
"@

# Shift, Ctrl, Alt, both Windows keys, and R: while any of them is held, Ctrl+C would arrive as Ctrl+Alt+C.
$Held = 0x10, 0x11, 0x12, 0x5B, 0x5C, 0x52

function Reply($obj) {
    [Console]::Out.WriteLine(($obj | ConvertTo-Json -Compress))
    [Console]::Out.Flush()
}

function Wait-KeysUp {
    $deadline = [DateTime]::UtcNow.AddSeconds(3)
    while ([DateTime]::UtcNow -lt $deadline) {
        $down = $false
        foreach ($vk in $Held) { if ([PetKeys]::GetAsyncKeyState($vk) -band 0x8000) { $down = $true; break } }
        if (-not $down) { return $true }
        Start-Sleep -Milliseconds 20
    }
    return $false
}

Reply @{ ok = $true; ready = $true }
while ($null -ne ($line = [Console]::In.ReadLine())) {
    $parts = $line.Trim() -split '\s+'
    try {
        switch ($parts[0]) {
            'grab' {
                if (-not (Wait-KeysUp)) { Reply @{ ok = $false; error = 'keys' }; break }
                $hwnd = [PetKeys]::GetForegroundWindow().ToInt64()
                [PetKeys]::Chord(0x43)   # C
                Reply @{ ok = $true; hwnd = "$hwnd" }
            }
            'paste' {
                $want = [int64]$parts[1]
                if ([PetKeys]::GetForegroundWindow().ToInt64() -ne $want) { Reply @{ ok = $false; error = 'moved' }; break }
                if (-not (Wait-KeysUp)) { Reply @{ ok = $false; error = 'keys' }; break }
                [PetKeys]::Chord(0x56)   # V
                Reply @{ ok = $true }
            }
            default { Reply @{ ok = $false; error = 'unknown command' } }
        }
    } catch {
        Reply @{ ok = $false; error = "$($_.Exception.Message)" }
    }
}
