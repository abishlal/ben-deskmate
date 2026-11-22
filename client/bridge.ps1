# Desktop bridge: lets the pet work with Microsoft To Do (through classic Outlook's task folders)
# and OneNote (desktop app) on this PC. The pet shell keeps one copy running and sends it one operation at a
# time, which saves starting PowerShell (about a second) for every call.
#
# Scope is deliberately narrow: task items, OneNote pages, and the calendar (read events, add a personal
# appointment). It never opens mail or contacts, never sends anything, never adds attendees, and refuses to
# change or delete any Outlook item that is not a task.
#
# Usage:  powershell -NoProfile -File bridge.ps1 -Op <operation>     (arguments as JSON on standard input)
#         powershell -NoProfile -File bridge.ps1 -Op serve           (one {"op":..,"args":..} JSON line in, one reply line out)
param([Parameter(Mandatory)][string]$Op)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)    # the shell sends UTF-8; the console default would garble it
$olTaskItem = 3; $olFolderTasks = 13; $olTaskClass = 48
$olAppointmentItem = 1; $olFolderCalendar = 9; $olAppointmentClass = 26
$NoDate = [datetime]'4501-01-01'

function Out-Result($data) { $script:Reply = @{ ok = $true; data = $data } | ConvertTo-Json -Depth 6 -Compress }
function Iso($d) { if ($d -and $d -lt $NoDate.AddDays(-1)) { $d.ToString('yyyy-MM-ddTHH:mm:ss') } else { $null } }

# ---------------------------------------------------------------- Outlook tasks (Microsoft To Do)
function Test-OutlookProfile {
    foreach ($v in '16.0', '15.0') {
        if (@(Get-ChildItem "HKCU:\Software\Microsoft\Office\$v\Outlook\Profiles" -ErrorAction SilentlyContinue).Count) { return $true }
    }
    return $false
}

function Get-Mapi {
    if (-not (Test-Path 'Registry::HKEY_CLASSES_ROOT\Outlook.Application\CLSID')) { throw 'Classic Outlook is not installed.' }
    if (-not (Test-OutlookProfile)) { throw 'Classic Outlook has no account yet. Open classic Outlook once and sign in, then try again.' }
    # An Outlook error box (e.g. "Outlook has exhausted all shared resources") blocks every call until it is closed:
    # say so at once instead of waiting for the call to time out.
    if (Get-Process OUTLOOK -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -eq 'Outlook Data File' }) {
        throw 'Classic Outlook is showing an error box. Close OneNote and Outlook (and the error box), then try again.'
    }
    $script:Outlook = New-Object -ComObject Outlook.Application
    $ns = $script:Outlook.GetNamespace('MAPI')
    try { $null = $ns.GetDefaultFolder($olFolderTasks).Name; return $ns }
    catch {
        # A hidden copy of Outlook started for the bridge (no window) sometimes loses its mailbox logon, e.g. after
        # sleep or a network change, and then every call fails or hangs until it is restarted. Restart it, but only
        # when it has no window open, so a copy you are working in is never closed; and at most every ten minutes.
        if ($script:Outlook.Explorers.Count -or $script:Outlook.Inspectors.Count) { throw }
        if ($script:OutlookRestarted -and ((Get-Date) - $script:OutlookRestarted).TotalMinutes -lt 10) { throw }
        $script:OutlookRestarted = Get-Date
        Restart-HiddenOutlook
        $script:Outlook = New-Object -ComObject Outlook.Application
        $ns = $script:Outlook.GetNamespace('MAPI')
        try { $null = $ns.GetDefaultFolder($olFolderTasks).Name; return $ns }
        catch { throw 'Classic Outlook cannot reach your mailbox. Open classic Outlook once to sign in again.' }
    }
}

function Restart-HiddenOutlook {
    try { $script:Outlook.Quit() } catch {}
    $script:Outlook = $null
    [GC]::Collect(); [GC]::WaitForPendingFinalizers()
    $hidden = @(Get-CimInstance Win32_Process -Filter "Name='OUTLOOK.EXE'" | Where-Object { $_.CommandLine -match '-Embedding' })
    foreach ($p in $hidden) {
        $proc = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue
        if (-not $proc) { continue }
        if (-not $proc.WaitForExit(15000) -and $proc.MainWindowHandle -eq 0) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    }
    Start-Sleep -Seconds 2
}

function Get-TaskFolders($ns) {
    # The default Tasks folder plus any other task lists in the same mailbox (To Do lists appear as task folders).
    $default = $ns.GetDefaultFolder($olFolderTasks)
    $folders = @($default)
    foreach ($parent in @($default, $default.Parent)) {
        foreach ($f in $parent.Folders) {
            if ($f.DefaultItemType -eq $olTaskItem -and $f.EntryID -ne $default.EntryID -and -not ($folders | Where-Object { $_.EntryID -eq $f.EntryID })) { $folders += $f }
        }
    }
    return $folders        # callers wrap this in @() because a single folder comes back unwrapped
}

function Get-Task($ns, $id) {
    $item = $ns.GetItemFromID($id)
    if ($item.Class -ne $olTaskClass) { throw 'That item is not a task.' }
    return $item
}

function Format-Task($t, $listName) {
    @{
        id = $t.EntryID; title = $t.Subject; list = $listName; done = [bool]$t.Complete
        priority = @('low', 'normal', 'high')[[int]$t.Importance]
        due = Iso $t.DueDate
        reminder = if ($t.ReminderSet) { Iso $t.ReminderTime } else { $null }
        created = Iso $t.CreationTime
        completed = if ($t.Complete) { Iso $t.DateCompleted } else { $null }
        details = if ($t.Body) { $t.Body.Substring(0, [Math]::Min(300, $t.Body.Length)) } else { '' }
    }
}

function Set-TaskFields($t, $a) {
    if ($a.PSObject.Properties['title']) { $t.Subject = [string]$a.title }
    if ($a.PSObject.Properties['details']) { $t.Body = [string]$a.details }
    if ($a.PSObject.Properties['priority']) { $t.Importance = [array]::IndexOf(@('low', 'normal', 'high'), [string]$a.priority) }
    if ($a.PSObject.Properties['due']) {
        if ($a.due) {
            $when = [datetime]::Parse([string]$a.due, [Globalization.CultureInfo]::InvariantCulture)
            $t.DueDate = $when.Date; $t.ReminderSet = $true; $t.ReminderTime = $when
        } else { $t.DueDate = $NoDate; $t.ReminderSet = $false }
    }
    if ($a.PSObject.Properties['done']) { if ($a.done) { $t.MarkComplete() } else { $t.Complete = $false } }
}

# ---------------------------------------------------------------- Outlook calendar
function Format-Event($e) {
    @{
        id = $e.EntryID; title = $e.Subject; start = Iso $e.Start; end = Iso $e.End; all_day = [bool]$e.AllDayEvent
        location = [string]$e.Location; is_meeting = ($e.MeetingStatus -ne 0)
        organizer = if ($e.MeetingStatus -ne 0) { [string]$e.Organizer } else { '' }
        busy = @('free', 'tentative', 'busy', 'out of office', 'working elsewhere')[[int]$e.BusyStatus]
    }
}

# ---------------------------------------------------------------- OneNote
function Get-OneNote {
    if (-not (Test-Path 'Registry::HKEY_CLASSES_ROOT\OneNote.Application\CLSID')) { throw 'The OneNote desktop app is not installed.' }
    return New-Object -ComObject OneNote.Application
}

function Get-Pages($on) {
    $xml = ''; $on.GetHierarchy('', 4, [ref]$xml)   # 4 = down to pages
    $doc = [xml]$xml
    $script:OneNs = $doc.DocumentElement.NamespaceURI
    $pages = foreach ($p in $doc.GetElementsByTagName('one:Page')) {
        if ($p.GetAttribute('isInRecycleBin') -eq 'true' -or $p.ParentNode.GetAttribute('isInRecycleBin') -eq 'true') { continue }
        @{ id = $p.GetAttribute('ID'); title = $p.GetAttribute('name'); section = $p.ParentNode.GetAttribute('name')
           changed = $p.GetAttribute('lastModifiedTime') }
    }
    return @($pages)
}

function Escape-Html([string]$s) { $s.Replace('&', '&amp;').Replace('<', '&lt;').Replace('>', '&gt;') }

# --- Note format ------------------------------------------------------------------------
# The pet writes and reads pages in one small markup:
#   "# ", "## ", "### "  headings (OneNote's Heading 1-3 styles)
#   "- "                 bullet; indent two spaces to put it under the line above
#   "[ ] ", "[x] "       OneNote To Do checkbox, open or ticked (also "- [ ] ")
#   anything else        a normal paragraph
$script:StyleLook = @{
    h1 = @{ font = 'Calibri Light'; fontSize = '16.0'; fontColor = '#1E4E79'; spaceBefore = '0.6' }
    h2 = @{ font = 'Calibri Light'; fontSize = '14.0'; fontColor = '#2E75B5'; spaceBefore = '0.4' }
    h3 = @{ font = 'Calibri Light'; fontSize = '12.0'; fontColor = '#1F3763'; spaceBefore = '0.3' }
    p  = @{ font = 'Calibri'; fontSize = '11.0'; fontColor = 'automatic'; spaceBefore = '0.0' }
}
$script:Boxes = "$([char]0x2610)$([char]0x2611)"     # the checkbox characters older versions of the bridge wrote

# A page's XML, with $script:Nsm and $script:StyleNames (quick-style index -> name) set for it.
function Get-PageDoc($on, [string]$id) {
    $xml = ''; $on.GetPageContent($id, [ref]$xml, 0)       # 0 = no binary data; outlines with images are never written back
    $doc = [xml]$xml
    $script:OneNs = $doc.DocumentElement.NamespaceURI
    $script:Nsm = New-Object Xml.XmlNamespaceManager $doc.NameTable
    $script:Nsm.AddNamespace('one', $script:OneNs)
    $script:StyleNames = @{}
    foreach ($d in $doc.DocumentElement.SelectNodes('one:QuickStyleDef', $script:Nsm)) { $script:StyleNames[$d.GetAttribute('index')] = $d.GetAttribute('name') }
    return , $doc     # the comma stops PowerShell from unrolling the document into its child nodes
}

function New-OneElement($doc, [string]$name) { $doc.CreateElement('one', $name, $script:OneNs) }

# The index of a TagDef or QuickStyleDef on the page, added (with the next free index) when it is missing.
function Get-DefIndex($doc, [string]$kind, [scriptblock]$match, [hashtable]$attrs) {
    $root = $doc.DocumentElement
    $defs = @($root.SelectNodes("one:$kind", $script:Nsm))
    $hit = $defs | Where-Object $match | Select-Object -First 1
    if ($hit) { return [int]$hit.GetAttribute('index') }
    $next = 0
    foreach ($d in $defs) { $next = [Math]::Max($next, [int]$d.GetAttribute('index') + 1) }
    $el = New-OneElement $doc $kind
    $el.SetAttribute('index', [string]$next)
    foreach ($k in $attrs.Keys) { $el.SetAttribute($k, [string]$attrs[$k]) }
    # TagDefs come first on a page, then QuickStyleDefs, then everything else.
    $before = $root.ChildNodes | Where-Object { $_.NodeType -eq 'Element' -and $_.LocalName -ne 'TagDef' -and ($kind -eq 'TagDef' -or $_.LocalName -ne 'QuickStyleDef') } | Select-Object -First 1
    if ($before) { $null = $root.InsertBefore($el, $before) } else { $null = $root.AppendChild($el) }
    return $next
}

function Get-StyleIndex($doc, [string]$name) {
    $look = $script:StyleLook[$name]
    $i = Get-DefIndex $doc 'QuickStyleDef' { $_.GetAttribute('name') -eq $name } @{
        name = $name; fontColor = $look.fontColor; highlightColor = 'automatic'; font = $look.font; fontSize = $look.fontSize
        spaceBefore = $look.spaceBefore; spaceAfter = '0.0'
    }
    $script:StyleNames[[string]$i] = $name
    return $i
}

function Get-TodoIndex($doc) {
    Get-DefIndex $doc 'TagDef' { $_.GetAttribute('symbol') -eq '3' } @{ type = '0'; symbol = '3'; fontColor = 'automatic'; highlightColor = 'none'; name = 'To Do' }
}

function Test-TodoTag($doc, $tag) {
    $def = $doc.DocumentElement.SelectSingleNode("one:TagDef[@index='$($tag.GetAttribute('index'))']", $script:Nsm)
    return [bool]($def -and ($def.GetAttribute('symbol') -eq '3' -or $def.GetAttribute('name') -match 'to ?do'))
}

# The text of one paragraph (OE), without formatting.
function Get-OEText($oe) {
    $parts = foreach ($t in $oe.SelectNodes('one:T', $script:Nsm)) { [Net.WebUtility]::HtmlDecode(($t.InnerText -replace '<[^>]+>', '')) }
    return (($parts -join '') -replace '\s+', ' ').Trim()
}

# 1-6 when the paragraph is a heading (a heading style, or the bold-only line older versions wrote), else 0.
function Get-OEHeading($oe) {
    $name = [string]$script:StyleNames[$oe.GetAttribute('quickStyleIndex')]
    if ($name -match '^h([1-6])$') { return [int]$Matches[1] }
    $t = $oe.SelectSingleNode('one:T', $script:Nsm)
    if ($t -and $t.InnerText.Trim() -match "^<span style='font-weight:bold'>[^<]*</span>$") { return 1 }
    return 0
}

function Get-HeadingKey([string]$s) { ($s -replace "^[#\s$($script:Boxes)]+", '' -replace '[\s:.]+$', '' -replace '\s+', ' ').Trim().ToLowerInvariant() }

# Turns markup into OEs: top-level ones go into $oec (before $before, or at the end); indented bullets and
# checkboxes go under the line above them.
function Add-MarkupOEs($doc, $oec, [string]$text, $before) {
    $stack = New-Object Collections.ArrayList        # open list items: @{ indent; oe }
    foreach ($raw in ($text -split "`r?`n")) {
        if (-not $raw.Trim()) { continue }
        $expanded = $raw -replace "`t", '  '
        $indent = $expanded.Length - $expanded.TrimStart().Length
        $line = $raw.Trim()
        $kind = 'p'; $content = $line; $done = $false
        if ($line -match '^(#{1,6})\s+(.*)$') {
            $kind = 'h' + [Math]::Min(3, $Matches[1].Length); $content = [string]$Matches[2]
        } else {
            if ($line -match '^[-*+]\s+(.*)$') { $kind = 'bullet'; $content = [string]$Matches[1] }
            if ($content -match "^(?:\[( |x|X)?\]|([$($script:Boxes)]))\s*(.*)$") {
                $mark = [string]$Matches[1]; $box = [string]$Matches[2]; $content = [string]$Matches[3]   # copy first: -match resets $Matches
                $kind = 'todo'; $done = ($mark -eq 'x' -or $mark -eq 'X' -or $box -eq [string][char]0x2611)
            }
        }
        $oe = New-OneElement $doc 'OE'
        $oe.SetAttribute('quickStyleIndex', [string](Get-StyleIndex $doc $(if ($kind -like 'h*') { $kind } else { 'p' })))
        if ($kind -eq 'todo') {
            $tag = New-OneElement $doc 'Tag'
            $tag.SetAttribute('index', [string](Get-TodoIndex $doc)); $tag.SetAttribute('completed', $(if ($done) { 'true' } else { 'false' }))
            $null = $oe.AppendChild($tag)
        } elseif ($kind -eq 'bullet') {
            $list = New-OneElement $doc 'List'; $bullet = New-OneElement $doc 'Bullet'
            $bullet.SetAttribute('bullet', '2'); $bullet.SetAttribute('fontSize', '11.0')
            $null = $list.AppendChild($bullet); $null = $oe.AppendChild($list)
        }
        $t = New-OneElement $doc 'T'
        $null = $t.AppendChild($doc.CreateCDataSection((Escape-Html $content)))
        $null = $oe.AppendChild($t)

        $container = $null
        if ($kind -in 'bullet', 'todo' -and $indent -gt 0) {
            while ($stack.Count -and $stack[$stack.Count - 1].indent -ge $indent) { $stack.RemoveAt($stack.Count - 1) }
            if ($stack.Count) {
                $owner = $stack[$stack.Count - 1].oe
                $container = $owner.SelectSingleNode('one:OEChildren', $script:Nsm)
                if (-not $container) { $container = New-OneElement $doc 'OEChildren'; $null = $owner.AppendChild($container) }
                $null = $container.AppendChild($oe)
            }
        }
        if (-not $container) {
            $stack.Clear()
            if ($before) { $null = $oec.InsertBefore($oe, $before) } else { $null = $oec.AppendChild($oe) }
        }
        if ($kind -in 'bullet', 'todo') { $null = $stack.Add(@{ indent = $indent; oe = $oe }) }
    }
}

# Adds markup to a page. All text goes into one block (the page's last text outline) instead of a new block
# per call. With $under, it goes at the end of the part under the heading with that text, and when there is
# no such heading, the heading is added first. Returns whether the heading was found.
function Add-NoteBody($on, [string]$id, [string]$text, [string]$under) {
    $doc = Get-PageDoc $on $id
    $root = $doc.DocumentElement
    # Outlines with pictures, ink or files are left alone: they are read without their binary data.
    $outlines = @($root.SelectNodes('one:Outline', $script:Nsm) | Where-Object {
        -not $_.SelectSingleNode('.//one:Image|.//one:InkDrawing|.//one:InkParagraph|.//one:InkWord|.//one:InsertedFile|.//one:MediaFile', $script:Nsm) })
    $target = $null; $before = $null; $found = $false
    if ($under) {
        $want = Get-HeadingKey $under
        foreach ($o in $outlines) {
            $kids = @($o.SelectNodes('one:OEChildren/one:OE', $script:Nsm))
            for ($i = 0; $i -lt $kids.Count -and -not $found; $i++) {
                if ((Get-HeadingKey (Get-OEText $kids[$i])) -ne $want) { continue }
                $found = $true; $target = $o
                $level = Get-OEHeading $kids[$i]; if (-not $level) { $level = 7 }
                for ($j = $i + 1; $j -lt $kids.Count; $j++) {
                    $l2 = Get-OEHeading $kids[$j]
                    if ($l2 -and $l2 -le $level) { $before = $kids[$j]; break }
                }
            }
            if ($found) { break }
        }
    }
    if (-not $target) { $target = $outlines | Select-Object -Last 1 }
    if (-not $target) { $target = New-OneElement $doc 'Outline'; $null = $root.AppendChild($target) }
    $oec = $target.SelectSingleNode('one:OEChildren', $script:Nsm)
    if (-not $oec) { $oec = New-OneElement $doc 'OEChildren'; $null = $target.AppendChild($oec) }
    $body = if ($under -and -not $found) { "# $($under.Trim())`n$text" } else { $text }
    Add-MarkupOEs $doc $oec $body $before
    $defs = ($root.SelectNodes('one:TagDef|one:QuickStyleDef', $script:Nsm) | ForEach-Object { $_.OuterXml }) -join ''
    $on.UpdatePageContent("<one:Page xmlns:one=`"$script:OneNs`" ID=`"$id`">$defs$($target.OuterXml)</one:Page>")
    return $found
}

# A page as markup (see the note format above), title first.
function Add-OELines($doc, $oec, [int]$depth, $lines) {
    if (-not $oec) { return }
    foreach ($oe in $oec.SelectNodes('one:OE', $script:Nsm)) {
        $text = Get-OEText $oe
        if ($text) {
            $pad = '  ' * $depth
            $tag = $oe.SelectSingleNode('one:Tag', $script:Nsm)
            $level = Get-OEHeading $oe
            if ($tag -and (Test-TodoTag $doc $tag)) {
                $lines.Add($pad + $(if ($tag.GetAttribute('completed') -eq 'true') { '[x] ' } else { '[ ] ' }) + $text)
            } elseif ($text -match "^([$($script:Boxes)])\s*(.*)$") {
                $lines.Add($pad + $(if ($Matches[1] -eq [string][char]0x2611) { '[x] ' } else { '[ ] ' }) + $Matches[2])
            } elseif ($oe.SelectSingleNode('one:List', $script:Nsm)) {
                $lines.Add("$pad- $text")
            } elseif ($level) {
                $lines.Add(('#' * $level) + " $text")
            } else {
                $lines.Add($pad + $text)
            }
        }
        foreach ($cell in $oe.SelectNodes('one:Table/one:Row/one:Cell/one:OEChildren', $script:Nsm)) { Add-OELines $doc $cell ($depth + 1) $lines }
        Add-OELines $doc $oe.SelectSingleNode('one:OEChildren', $script:Nsm) ($depth + 1) $lines
    }
}

function Get-Sections($on) {
    $xml = ''; $on.GetHierarchy('', 4, [ref]$xml)
    $found = foreach ($s in ([xml]$xml).GetElementsByTagName('one:Section')) {
        if ($s.GetAttribute('isInRecycleBin') -eq 'true' -or $s.GetAttribute('isRecycleBin') -eq 'true') { continue }
        $nb = $s.ParentNode; while ($nb -and $nb.LocalName -ne 'Notebook') { $nb = $nb.ParentNode }
        if ($s.ParentNode.GetAttribute('isRecycleBin') -eq 'true') { continue }
        @{ id = $s.GetAttribute('ID'); section = $s.GetAttribute('name'); notebook = if ($nb) { $nb.GetAttribute('name') } else { '' }
           notebookId = if ($nb) { $nb.GetAttribute('ID') } else { '' }
           pages = @($s.ChildNodes | Where-Object { $_.LocalName -eq 'Page' }).Count }
    }
    return @($found)
}

# The notebook chosen in .env (PET_ONENOTE_NOTEBOOK): its name or part of its path. Two notebooks can have the
# same name (OneNote makes one of its own for Quick Notes), so a path match wins, then the larger notebook.
function Get-TargetNotebook($on) {
    $want = ([string]$env:PET_ONENOTE_NOTEBOOK).Trim()
    if (-not $want) { return $null }
    $norm = { param($p) ([string]$p).Replace('\', '/').TrimEnd('/').ToLowerInvariant() }
    $w = & $norm $want
    $xml = ''; $on.GetHierarchy('', 3, [ref]$xml)
    $nbs = @(([xml]$xml).GetElementsByTagName('one:Notebook'))
    $byPath = @($nbs | Where-Object { (& $norm $_.GetAttribute('path')).EndsWith($w) -or (& $norm $_.GetAttribute('path')).Contains($w) })
    $byName = @($nbs | Where-Object { $_.GetAttribute('name') -eq $want -or $_.GetAttribute('nickname') -eq $want })
    $pick = if ($byPath) { $byPath } else { $byName }
    if (-not $pick) { throw "No open OneNote notebook matches '$want' (PET_ONENOTE_NOTEBOOK)." }
    return ($pick | Sort-Object { @($_.GetElementsByTagName('one:Section')).Count } -Descending | Select-Object -First 1)
}

# A section by name, looked for in the chosen notebook first; with $create, made there when it is missing.
function Find-Section($on, [string]$name, [bool]$create = $false) {
    $target = Get-TargetNotebook $on
    $all = @(Get-Sections $on)
    if ($target) {
        $hit = $all | Where-Object { $_.section -eq $name -and $_.notebookId -eq $target.GetAttribute('ID') } | Select-Object -First 1
        if ($hit) { return $hit }
        if ($create) {
            $id = ''; $on.OpenHierarchy("$name.one", $target.GetAttribute('ID'), [ref]$id, 3)   # 3 = create the section
            return @{ id = $id; section = $name; notebook = $target.GetAttribute('name'); notebookId = $target.GetAttribute('ID'); created = $true }
        }
    }
    return ($all | Where-Object { $_.section -eq $name } | Select-Object -First 1)
}

function Get-PageText($on, $id) {
    $doc = Get-PageDoc $on $id
    $lines = New-Object Collections.Generic.List[string]
    $title = $doc.DocumentElement.SelectSingleNode('one:Title/one:OE', $script:Nsm)
    if ($title -and (Get-OEText $title)) { $lines.Add((Get-OEText $title)) }
    foreach ($o in $doc.DocumentElement.SelectNodes('one:Outline', $script:Nsm)) {
        Add-OELines $doc $o.SelectSingleNode('one:OEChildren', $script:Nsm) 0 $lines
    }
    return ($lines -join "`n")
}

function Get-DefaultSectionId($on) {
    if (Get-TargetNotebook $on) {
        $name = ([string]$env:PET_ONENOTE_SECTION).Trim(); if (-not $name) { $name = 'Quick Notes' }
        return (Find-Section $on $name $true).id
    }
    try {
        $path = ''; $on.GetSpecialLocation(1, [ref]$path)       # 1 = the "quick notes" section
        $id = ''; $on.OpenHierarchy($path, '', [ref]$id, 0)
        if ($id) { return $id }
    } catch {}
    $xml = ''; $on.GetHierarchy('', 3, [ref]$xml)
    $first = ([xml]$xml).GetElementsByTagName('one:Section') | Where-Object { $_.GetAttribute('isInRecycleBin') -ne 'true' } | Select-Object -First 1
    if (-not $first) { throw 'OneNote has no section to put a new page in.' }
    return $first.GetAttribute('ID')
}

# ---------------------------------------------------------------- operations
function Invoke-Op([string]$Op, $a) {
    switch ($Op) {
        'status' {
            # Cheap check that starts neither app.
            Out-Result @{
                tasks = (Test-Path 'Registry::HKEY_CLASSES_ROOT\Outlook.Application\CLSID') -and (Test-OutlookProfile)
                notes = (Test-Path 'Registry::HKEY_CLASSES_ROOT\OneNote.Application\CLSID')
                calendar = (Test-Path 'Registry::HKEY_CLASSES_ROOT\Outlook.Application\CLSID') -and (Test-OutlookProfile)
            }
        }
        'tasks_list' {
            $ns = Get-Mapi
            $found = foreach ($folder in @(Get-TaskFolders $ns)) {
                foreach ($t in $folder.Items.Restrict('[Complete] = False')) { if ($t.Class -eq $olTaskClass) { Format-Task $t $folder.Name } }
            }
            Out-Result @($found)
        }
        'tasks_done' {
            # Read-only: tasks completed on or after a date, for the wrap-up and the weekly report.
            $ns = Get-Mapi
            $since = ([datetime]::Parse([string]$a.since, [Globalization.CultureInfo]::InvariantCulture)).Date
            $found = New-Object Collections.ArrayList
            foreach ($folder in @(Get-TaskFolders $ns)) {
                try { $items = $folder.Items.Restrict("[Complete] = True AND [DateCompleted] >= '" + $since.AddDays(-1).ToString('d') + "'") }
                catch { $items = $folder.Items.Restrict('[Complete] = True') }
                foreach ($t in $items) {
                    if ($t.Class -ne $olTaskClass -or $t.DateCompleted -lt $since -or $t.DateCompleted -ge $NoDate.AddDays(-1)) { continue }
                    $null = $found.Add((Format-Task $t $folder.Name))
                    if ($found.Count -ge 200) { break }
                }
            }
            Out-Result @($found)
        }
        'task_add' {
            $ns = Get-Mapi
            $folders = @(Get-TaskFolders $ns)
            $folder = $folders[0]
            if ($a.PSObject.Properties['list'] -and $a.list) {
                $folder = $folders | Where-Object { $_.Name -eq [string]$a.list } | Select-Object -First 1
                if (-not $folder) { throw ('No list with that name. Lists: ' + (($folders | ForEach-Object Name) -join ', ')) }
            }
            $t = $folder.Items.Add($olTaskItem)
            Set-TaskFields $t $a
            $t.Save()
            Out-Result (Format-Task $t $folder.Name)
        }
        'task_update' {
            $ns = Get-Mapi
            $t = Get-Task $ns ([string]$a.id)
            Set-TaskFields $t $a
            $t.Save()
            Out-Result (Format-Task $t $t.Parent.Name)
        }
        'task_delete' {
            $ns = Get-Mapi
            (Get-Task $ns ([string]$a.id)).Delete()      # goes to Outlook's Deleted Items
            Out-Result $true
        }
        'notes_search' {
            $on = Get-OneNote
            $pages = Get-Pages $on
            $query = if ($a.PSObject.Properties['query']) { ([string]$a.query).Trim() } else { '' }
            if ($query) {
                $hits = @{}
                foreach ($p in $pages) { if ($p.title -like "*$query*") { $hits[$p.id] = $true } }
                try {   # full-text search; needs Windows Search indexing, so it is best effort
                    $xml = ''; $on.FindPages('', $query, [ref]$xml)
                    foreach ($p in ([xml]$xml).GetElementsByTagName('one:Page')) { $hits[$p.GetAttribute('ID')] = $true }
                } catch {}
                $pages = @($pages | Where-Object { $hits[$_.id] })
            }
            Out-Result @($pages | Sort-Object { $_.changed } -Descending | Select-Object -First 15)
        }
        'note_read' {
            $on = Get-OneNote
            Out-Result (Get-PageText $on ([string]$a.id))
        }
        'note_create' {
            $on = Get-OneNote
            $null = Get-Pages $on
            $sectionName = if ($env:PET_ONENOTE_SECTION) { [string]$env:PET_ONENOTE_SECTION } else { 'Quick Notes' }
            if ($a.PSObject.Properties['section'] -and $a.section) {
                $match = Find-Section $on ([string]$a.section)
                if (-not $match) { throw ('No section with that name. Sections: ' + ((@(Get-Sections $on) | ForEach-Object { $_.section } | Select-Object -Unique) -join ', ')) }
                $section = $match.id; $sectionName = $match.section
            } else { $section = Get-DefaultSectionId $on }
            # With "parent", the page goes in as a sub-page of the page with that title (made, title only, if missing),
            # after any sub-pages it already has: e.g. the month "Nov, 2026" under the year "2026".
            $parentTitle = if ($a.PSObject.Properties['parent']) { ([string]$a.parent).Trim() } else { '' }
            if ($parentTitle) {
                $sx = ''; $on.GetHierarchy($section, 4, [ref]$sx)
                $hasParent = @(([xml]$sx).GetElementsByTagName('one:Page') | Where-Object { $_.GetAttribute('name') -eq $parentTitle }).Count
                if (-not $hasParent) {
                    $parentId = ''; $on.CreateNewPage($section, [ref]$parentId, 0)
                    $on.UpdatePageContent("<one:Page xmlns:one=`"$script:OneNs`" ID=`"$parentId`"><one:Title><one:OE><one:T><![CDATA[$(Escape-Html $parentTitle)]]></one:T></one:OE></one:Title></one:Page>")
                }
            }
            $id = ''; $on.CreateNewPage($section, [ref]$id, 0)
            $on.UpdatePageContent("<one:Page xmlns:one=`"$script:OneNs`" ID=`"$id`"><one:Title><one:OE><one:T><![CDATA[$(Escape-Html ([string]$a.title))]]></one:T></one:OE></one:Title></one:Page>")
            $null = Add-NoteBody $on $id ([string]$a.body) ''
            if ($parentTitle) {
                $sx = ''; $on.GetHierarchy($section, 4, [ref]$sx); $sd = [xml]$sx
                $pages = @($sd.GetElementsByTagName('one:Page'))
                $parent = $pages | Where-Object { $_.GetAttribute('name') -eq $parentTitle } | Select-Object -Last 1
                $me = $pages | Where-Object { $_.GetAttribute('ID') -eq $id } | Select-Object -First 1
                if ($parent -and $me) {
                    $level = [int]$parent.GetAttribute('pageLevel')
                    $after = $parent; $n = $parent.NextSibling
                    while ($n -and $n.LocalName -eq 'Page' -and $n.GetAttribute('ID') -ne $id -and [int]$n.GetAttribute('pageLevel') -gt $level) { $after = $n; $n = $n.NextSibling }
                    $me.SetAttribute('pageLevel', [string]($level + 1))
                    if ($after.GetAttribute('ID') -ne $id) { $null = $me.ParentNode.RemoveChild($me); $null = $after.ParentNode.InsertAfter($me, $after) }
                    $on.UpdateHierarchy($sd.OuterXml)
                }
            }
            Out-Result @{ id = $id; title = [string]$a.title; section = $sectionName }
        }
        'note_section_ensure' {
            # The section with this name (used for the work log): in the notebook chosen in .env, made there if missing;
            # without that setting, any section with the name, else a new one in the first open notebook.
            $on = Get-OneNote
            $name = ([string]$a.name).Trim()
            if (-not $name -or $name -match '[\\/:*?"<>|]') { throw 'Give a plain section name.' }
            if (Get-TargetNotebook $on) {
                $s = Find-Section $on $name $true
                Out-Result @{ section = $s.section; notebook = $s.notebook; created = [bool]$s.created }; return
            }
            $found = @(Get-Sections $on) | Where-Object { $_.section -eq $name } | Select-Object -First 1
            if ($found) { Out-Result @{ section = $found.section; notebook = $found.notebook; created = $false }; return }
            $xml = ''; $on.GetHierarchy('', 1, [ref]$xml)          # 1 = notebooks
            $nb = ([xml]$xml).GetElementsByTagName('one:Notebook') | Select-Object -First 1
            if (-not $nb) { throw 'OneNote has no open notebook.' }
            $id = ''; $on.OpenHierarchy("$name.one", $nb.GetAttribute('ID'), [ref]$id, 3)   # 3 = create the section
            Out-Result @{ section = $name; notebook = $nb.GetAttribute('name'); created = $true }
        }
        'note_sections' {
            $on = Get-OneNote
            Out-Result @(Get-Sections $on | ForEach-Object { @{ notebook = $_.notebook; section = $_.section; pages = $_.pages } })
        }
        'calendar_list' {
            # Read-only: events between two local times, recurring ones expanded.
            $ns = Get-Mapi
            $from = [datetime]::Parse([string]$a.start, [Globalization.CultureInfo]::InvariantCulture)
            $to = [datetime]::Parse([string]$a.end, [Globalization.CultureInfo]::InvariantCulture)
            $items = $ns.GetDefaultFolder($olFolderCalendar).Items
            $items.Sort('[Start]'); $items.IncludeRecurrences = $true
            $hits = $items.Restrict("[Start] < '" + $to.ToString('g') + "' AND [End] > '" + $from.ToString('g') + "'")
            $events = New-Object Collections.ArrayList
            foreach ($e in $hits) {                 # never use .Count here: with recurrences it is unbounded
                if ($e.Class -ne $olAppointmentClass) { continue }
                if ($e.Start -ge $to -or $e.End -le $from) { continue }
                $null = $events.Add((Format-Event $e))
                if ($events.Count -ge 60) { break }
            }
            Out-Result @($events)
        }
        'calendar_add' {
            # A personal appointment only: no attendees are added and nothing is ever sent.
            $ns = Get-Mapi
            $e = $script:Outlook.CreateItem($olAppointmentItem)
            $e.Subject = [string]$a.title
            $e.Start = [datetime]::Parse([string]$a.start, [Globalization.CultureInfo]::InvariantCulture)
            $e.End = [datetime]::Parse([string]$a.end, [Globalization.CultureInfo]::InvariantCulture)
            if ($a.PSObject.Properties['location']) { $e.Location = [string]$a.location }
            if ($a.PSObject.Properties['details']) { $e.Body = [string]$a.details }
            $e.ReminderSet = $true; $e.ReminderMinutesBeforeStart = 10
            $e.Save()
            Out-Result (Format-Event $e)
        }
        'note_append' {
            # Adds to the page's text (at the end, or with "under" at the end of that heading's part); never replaces it.
            $on = Get-OneNote
            $under = if ($a.PSObject.Properties['under']) { ([string]$a.under).Trim() } else { '' }
            $found = Add-NoteBody $on ([string]$a.id) ([string]$a.body) $under
            Out-Result @{ under_found = [bool]$found }
        }
        'note_delete' {
            $on = Get-OneNote
            $on.DeleteHierarchy([string]$a.id)           # goes to the notebook's recycle bin
            Out-Result $true
        }
        default { throw "Unknown operation '$Op'." }
    }
}

function Invoke-Request([string]$Op, [scriptblock]$getArgs) {
    $script:Reply = $null
    try {
        $a = & $getArgs
        if ($null -eq $a) { $a = [pscustomobject]@{} }
        $null = Invoke-Op $Op $a
        if (-not $script:Reply) { throw 'The bridge returned nothing.' }
        return $script:Reply
    } catch {
        return (@{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress)
    } finally {
        # Let go of Outlook and OneNote between calls, so a long-running bridge never keeps them open.
        $script:Outlook = $null
        [GC]::Collect(); [GC]::WaitForPendingFinalizers()
    }
}

if ($Op -ne 'serve') {
    [Console]::Out.Write((Invoke-Request $Op { $raw = [Console]::In.ReadToEnd(); if ($raw.Trim()) { $raw | ConvertFrom-Json } }))
    return
}
while ($null -ne ($line = [Console]::In.ReadLine())) {
    if (-not $line.Trim()) { continue }
    try { $req = $line | ConvertFrom-Json } catch { $req = $null }
    $reply = if ($req -and $req.op) { Invoke-Request ([string]$req.op) { $req.args } }
             else { @{ ok = $false; error = 'Malformed request.' } | ConvertTo-Json -Compress }
    [Console]::Out.Write($reply + "`n")
    [Console]::Out.Flush()
}
