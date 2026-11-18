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
    $script:Outlook = New-Object -ComObject Outlook.Application
    return $script:Outlook.GetNamespace('MAPI')
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

function New-OutlineXml([string]$text) {
    $lines = @($text -split "`r?`n" | Where-Object { $_.Trim() })
    if (-not $lines.Count) { $lines = @('') }
    # "- " makes a bullet, "[ ] " a checkbox line, "# " a bold heading; anything else is a paragraph.
    $oes = ($lines | ForEach-Object {
        $line = $_.Trim()
        if ($line -match '^[-*]\s+(.*)$') {
            "<one:OE><one:List><one:Bullet bullet=`"2`" fontSize=`"11.0`"/></one:List><one:T><![CDATA[$(Escape-Html $Matches[1])]]></one:T></one:OE>"
        } elseif ($line -match '^\[( |x|X)?\]\s*(.*)$') {
            $mark = [string]$Matches[1]; $rest = [string]$Matches[2]     # copy first: the next -match resets $Matches
            $box = if ($mark -match 'x') { [char]0x2611 } else { [char]0x2610 }
            "<one:OE><one:T><![CDATA[$box $(Escape-Html $rest)]]></one:T></one:OE>"
        } elseif ($line -match '^#+\s+(.*)$') {
            "<one:OE><one:T><![CDATA[<span style='font-weight:bold'>$(Escape-Html $Matches[1])</span>]]></one:T></one:OE>"
        } else {
            "<one:OE><one:T><![CDATA[$(Escape-Html $line)]]></one:T></one:OE>"
        }
    }) -join ''
    return "<one:Outline><one:OEChildren>$oes</one:OEChildren></one:Outline>"
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
    $xml = ''; $on.GetPageContent($id, [ref]$xml, 0)
    $doc = [xml]$xml
    $lines = foreach ($t in $doc.GetElementsByTagName('one:T')) {
        $plain = [Net.WebUtility]::HtmlDecode(($t.InnerText -replace '<[^>]+>', '')).Trim()
        if ($plain) { $plain }
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
            $page = "<one:Page xmlns:one=`"$script:OneNs`" ID=`"$id`"><one:Title><one:OE><one:T><![CDATA[$(Escape-Html ([string]$a.title))]]></one:T></one:OE></one:Title>$(New-OutlineXml ([string]$a.body))</one:Page>"
            $on.UpdatePageContent($page)
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
            $on = Get-OneNote
            $null = Get-Pages $on
            $on.UpdatePageContent("<one:Page xmlns:one=`"$script:OneNs`" ID=`"$([string]$a.id)`">$(New-OutlineXml ([string]$a.body))</one:Page>")
            Out-Result $true
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
