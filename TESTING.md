# Torrent Dashboard Testing Guide

Automated checks are the release gate; this document records the manual verification that still depends on a browser, qBitTorrent instance, operating system, or update/restart behavior.

## Automated baseline

Run before and after a development increment:

```bash
python release_tools/validate_source.py
python release_tools/validate_ui_strings.py
node --check static/app.js
node --check static/settings.js
```

For a release candidate also verify generated documentation:

```bash
python release_tools/generate_release_notes.py --version X.Y.Z --check
```

`.github/workflows/validate.yml` runs these checks on pull requests and main for Linux/Windows with Python 3.13 and 3.14, using read-only repository permissions. It does not publish releases.

The Python suite uses only the standard library and currently covers extracted domain behavior, configuration transactions, release/provenance parsing and persistence, and source/architecture contracts.

## Manual smoke-test matrix

Only run tests that are relevant and safe in your environment. Never commit real credentials or private infrastructure details while documenting results.

### Startup and setup

- Fresh installation opens the first-run wizard.
- Setup completes with a reachable qBitTorrent instance.
- A failed client connection does not save partial setup state.
- Restarting after setup returns to the dashboard rather than the wizard.

### Authentication and roles

- Administrator login succeeds and administrative Settings/actions are available.
- Standard User login can view permitted dashboard/account surfaces and cannot perform administrator-only mutations.
- Logout invalidates the current session.
- Trusted-network behavior matches the configured access mode.

### Dashboard live state

- Dashboard refreshes without visibly reloading the page.
- Download/upload metrics and torrent counts reflect qBitTorrent state.
- Empty-state copy matches the real reason no rows are visible.
- Connection failures use the error banner and recover when connectivity returns.
- Search, category, tag, tracker, and status filters remain stable while polling.

### Torrent actions

- Start/resume, pause/stop, recheck, delete, force start, and location actions behave as expected for an administrator.
- Bulk actions operate only on selected torrents.
- Destructive actions require the expected confirmation.
- Standard Users cannot invoke administrator-only torrent mutations.

### Torrent details

- The docked torrent-details bar is always present on desktop/tablet and starts expanded with structural placeholders when no torrent is selected; mobile starts collapsed.
- Clicking the disclosure bar expands/collapses the inspector without clearing the selected torrent.
- Selecting a torrent automatically expands the inspector and updates the selected-torrent context.
- Expanding with no torrent selected shows the empty detail state without errors.
- Torrent list and detail body scroll independently when needed.
- General, Trackers, Peers, HTTP Sources, and Content tabs render without errors.
- Mobile keeps the persistent collapsed bar and uses the bottom-sheet presentation when expanded.

### Add Torrent

- Magnet/URL submission follows the normal qBitTorrent add path.
- `.torrent` upload follows the normal upload path.
- Metadata preview remains read-only and does not alter the source submitted to qBitTorrent.
- Metadata timeout/cancel paths leave the dialog usable.
- Save-as-`.torrent` is available only when metadata export is actually available.

### Settings

- General, Access, Clients, Updates, and Notifications use the shared **Settings saved** confirmation.
- User and Integration CRUD use scoped success/error language.
- Secret masks do not expose configured credentials back to the browser.
- Concurrent unrelated configuration changes do not overwrite one another.

### Updates and recovery

Use only a test installation or an environment where restart/rollback is acceptable.

- Check for updates discovers the intended public repository release.
- Patch notes show the current and previous release entries.
- Package SHA-256 values populate from authoritative release metadata.
- Downloaded update ZIP is verified before staging.
- Successful update restarts into the expected version.
- Runtime `config.json` and `data/` survive the update.
- A deliberately invalid test build rolls back rather than leaving the installation unusable.
- The authenticated dashboard has no Console navigation/view and `/api/recovery/command` is not exposed.
- `Recovery.exe` accepts the setup recovery key, lists configured clients/integrations/users/events, and can fetch live torrent identifiers directly from qBittorrent.
- `Recovery.exe` client/integration tests and Jellyfin task listing work without importing or starting the dashboard HTTP application.
- Recovery torrent mutations accept only start, stop, recheck, and reannounce; destructive torrent deletion is rejected.
- Recovery update download retains a verified package without installing it; update install revalidates that retained package before replacement, and update apply performs both steps.

### Responsive interface

At minimum check one desktop width and one mobile width after meaningful UI changes.

Desktop/tablet:

- Text remains legible at 100% browser zoom.
- Torrent workspace does not consume the entire page when few/no torrents are present.
- Torrent list plus open detail inspector fit in the initial viewport by default.
- No controls overlap at common desktop widths.

Mobile:

- Navigation remains reachable.
- Tables/cards do not introduce unusable horizontal overflow.
- Torrent detail bar remains reachable, expands as a bottom sheet, and collapses again with a full-width touch target.
- Dialogs remain operable with the on-screen keyboard present.

## Recording gaps

If a regression cannot reasonably be automated yet, record the missing coverage in `development/current.json` or the next release metadata and add it to this matrix if it is a recurring verification need.

Do not use this file as a test-results log; it is a stable testing contract for upstream and forks.

### Update-check intent and empty detail disclosure

- Open Settings → Updates and verify no GitHub update request is initiated solely by entering the page; cached/local release history may render immediately.
- Press Check for updates and verify the normal GitHub update lookup occurs and refreshes update/release-integrity information.
- With no torrent selected, verify the collapsed Torrent details bar contains no “No torrent selected” helper text.
- Select a torrent and verify the selected torrent name may appear in the disclosure context and the inspector expands normally.


### Server-selection defaults

- With exactly one enabled qBitTorrent client, verify that client is selected automatically and All servers is not offered in the server selector.
- With one enabled client, verify Add Torrent and other client-specific actions are immediately available without first changing the server selector.
- With two or more enabled clients, verify All servers is available as an aggregation choice.
- With multiple clients, select a specific client, reload the dashboard, and verify the valid previous selection is restored.
- Disable or remove the remembered client and verify the dashboard falls back to All servers when multiple clients remain, or automatically selects the sole remaining enabled client.


### Product language and capitalization

- Verify setup, sign-in, Dashboard, Settings, Add torrent, account, and client-settings surfaces after a copy-system change.
- Confirm named destinations retain their intended label casing while headings, labels, actions, statuses, errors, and explanatory copy use natural sentence case.
- Confirm qBitTorrent, Torrent Dashboard, GitHub, API, Web API, HTTP, IP, URL, SHA-256, and `.torrent` keep their established capitalization.
- Confirm authored copy does not visibly change after JavaScript initializes or after dynamically generated controls are inserted.
- Verify access controls say **Allowed IP addresses** rather than whitelist language and client-management actions use **client** where that is the user-facing concept.
- Verify validation and toast messages contain no camelCase tokens, internal field names, or mechanically recased technical terms.


### Add Torrent source modes and file selection

- Open Add Torrent and verify **Magnet link** and **.torrent file** are separate source tabs; switching tabs never causes the inactive source to be submitted.
- In Magnet link mode, paste one magnet URI and verify metadata loads without adding the torrent. Once ready, verify the Content panel shows selectable folders/files.
- Select and clear a folder and verify all descendant files follow it. Create a mixed selection and verify the parent folder and Select all controls show an indeterminate state.
- Change an included file between Normal, High, and Maximum priority, add the torrent, and verify qBitTorrent receives the corresponding file priorities. Unchecked files must arrive as Do not download.
- In .torrent file mode, click the drop area and verify the platform file picker opens. Repeat by dragging a `.torrent` file onto the drop area and verify the selected filename is shown and metadata is parsed.
- Add a parsed `.torrent` with at least one excluded file and verify the torrent is added through qBitTorrent's cached metadata with the selected files honored.
- Force metadata parsing to fail or use an older qBitTorrent build and verify a local `.torrent` can still fall back to direct upload, without selectable file priorities.
- After magnet metadata is ready, use **Save .torrent file** and verify a non-empty `.torrent` downloads. Repeat with a source whose torrent already exists in qBitTorrent and verify export fallback succeeds.
- In .torrent file mode, use **Save .torrent file** and verify the originally selected local file is downloaded without requiring qBitTorrent metadata cache availability.

### Add Torrent hierarchy and detail-selection reconciliation

- Load a multi-folder torrent in Add Torrent and verify every folder/file checkbox remains vertically aligned in the same selection column.
- Verify folder rows use the locally embedded Material disclosure icon and file rows reserve an equal-width spacer. A child file label must begin to the right of its parent folder label; deeper descendants should continue stepping right by hierarchy depth.
- Verify the Name header is left-aligned at the beginning of the name column, Size and Priority remain aligned, folder rows do not show descendant file counts, and the preview summary appears without a redundant Content heading.
- Select a torrent row and verify Torrent details expands for it. The disclosure bar should identify the selected torrent, and the expanded panel should proceed directly to the detail tabs without repeating the torrent title/hash in a second header.
- Click the same torrent row again and verify the selected-row treatment clears and Torrent details returns to the no-selection shell, expanded on desktop and collapsed on mobile.
- Select one torrent and then a different torrent; verify details switch directly to the second torrent rather than clearing first.
- With a torrent selected in Torrent details, remove that torrent (or remove it directly in qBitTorrent) and verify the next status refresh clears the stale detail context and returns to the same no-selection shell. Removing another torrent must not clear the current detail selection.


### Customizable torrent columns

- With no saved layout, verify the desktop/tablet columns appear in the default order (Name, Size, Status, Progress, Seeds, Peers, Down, Up, ETA, Ratio, Category, Tags) with the default proportions. Selection stays on the far left and there is no dedicated Actions column.
- Drag a header (for example ETA) onto another position. The accent drop bar must track the pointer, the header and every row must show the new order on release, and the sort must not change.
- Click a header without moving and verify it still sorts; keyboard-activate headers and verify `aria-sort` follows the active direction.
- Drag Name's right-edge divider well to the right. Name must widen by the full drag distance while columns to its left do not move; columns to the right shrink nearest-first and stop at their minimums. Drag it back and verify the immediate right neighbour regains the width. Drag far left and verify Name stops at its minimum.
- After every move/resize, verify there is no horizontal scrollbar on the torrent viewport or the page. Resize the browser across several desktop/tablet widths and verify the table keeps fitting.
- Wait through several one-second refreshes and reload the page; the order and widths must persist without column jumps.
- Focus a header and use Alt+Left/Right (move) and Alt+Shift+Left/Right (resize).
- Double-click a divider and verify only widths reset. Right-click the header and verify Reset column widths, Reset column order, and Reset columns each do what they say.
- Seed `tdTorrentLayout` with malformed JSON, unknown column names, duplicates, or a missing column and verify the table falls back to a valid layout. Seed the retired `tdColumns` key and verify it is discarded.
- Right-click several torrent rows and verify the shared torrent context menu (not the header menu) opens at the pointer location.
- Verify header labels match body alignment: compact numeric columns are right-aligned and the remaining columns are left-aligned. The sort chevron must not shift the visible column boundary.
- At the mobile breakpoint with a customized desktop layout saved, verify the torrent cards keep the default field order, show no resize dividers, and have no desktop inline widths. Widen back past the breakpoint and verify the custom order returns immediately. Long-press a non-control area of a torrent card for roughly half a second and verify the same torrent context menu opens; move/scroll before the threshold and verify no menu opens. A normal tap must still open Torrent details, while the tap following a completed long press must not.
- On mobile cards, verify Size, Status, Seeds, Peers, Download, Upload, ETA, Ratio, Category, and Tags all keep their field label at the left edge and their value at the right edge; desktop right-alignment rules must not move numeric labels toward the center divider.
- On mobile, check a torrent while Torrent details is collapsed and verify the bulk action bar is fully visible above the disclosure bar and mobile navigation. Expand Torrent details while the torrent remains checked and verify the bulk action bar moves above the expanded sheet instead of being covered by it.

### Compact mobile torrent cards

- At 820 px and below, verify each torrent card uses two metadata columns: Size/Status, Seeds/Peers, Download/Upload, ETA/Ratio, and Category/Tags. Name and Progress must span the full card width.
- Verify the selection checkbox is overlaid at the card's top-right and no longer consumes its own full-width row.
- Verify long torrent names wrap to no more than two lines and do not force horizontal overflow.
- Verify each compact metadata item keeps its label left and its value right, including the Status pill and long Category/Tags values.
- Compare several cards with v0.5.104-sized content and verify substantially more than one torrent can fit in a typical phone viewport while preserving all displayed metadata.
- Recheck long-press row actions, normal tap-to-open Torrent details, checkbox selection, and the mobile bulk-action/detail-pane stacking behavior after the grid compaction.

### Responsive tracker and peer details

- At 820 px and below, open Peers and verify each connected peer is a compact labeled record: address heading, client beneath it, then Progress, Download, and Upload metrics. No anonymous vertical value stacks should remain.
- Open Trackers and verify each record shows a cleaned tracker name/URL, a human-readable status badge, labeled Seeds and Peers counts, and a Message section only when the tracker reports one.
- Verify qBitTorrent tracker statuses 0 through 4 render as Disabled, Not contacted, Working, Updating, and Not working rather than raw numeric codes.
- Verify pseudo-trackers such as DHT, PeX, and LSD do not display literal surrounding `**` markers.
- Test a long IPv6 peer address, long client name, long tracker URL, and long tracker message; records must wrap or ellipsize without horizontal overflow.
- Above 820 px, verify Peers and Trackers remain conventional tables with visible column headers.
- Recheck General before and after switching through Trackers and Peers; its layout and content must remain unchanged.

### Viewport-proportional desktop torrent workspace

- At the reported approximately 771 px desktop viewport, expand General and verify the torrent list occupies roughly the same visual share as the accepted reference layout (about 44% of the usable workspace) while General remains fully visible.
- Repeat at common desktop heights such as 768/800, 900, 1080, 1200, 1440, and taller displays; verify the list scales with the usable viewport instead of stopping at six rows on large screens.
- Verify the detail pane has priority over the preferred ratio: if General needs more height, the list reduces to the largest whole-row height that preserves the detail content, with three rows as the minimum desktop list.
- Verify the list height is always the table header plus a whole number of rendered rows; no partial torrent row may be clipped at the bottom.
- Switch comfortable/compact density and resize the browser height; verify the proportional target is recalculated from live row/header/detail measurements.
- Scroll the document without resizing and verify one-second polling does not change the list height merely because the workspace moved within the viewport.
- Verify Trackers, Peers, HTTP sources, and Content retain their bounded/internal-scroll behavior and mobile remains unchanged.


### Inline torrent sort indicator grouping

Manual regression coverage for desktop torrent headers:

1. Verify Name, Status, Progress, Category, and Tags show the sort chevron immediately after the label when hovered/focused/active.
2. Verify Size, Seeds, Peers, Down, Up, ETA, and Ratio remain right-aligned while their chevron appears immediately after the label.
3. Sort ascending and descending through both text and numeric columns and confirm the indicator never jumps to a column boundary.
4. Confirm fixed column widths, viewport-proportional workspace sizing, and mobile torrent cards are unchanged.


### Persistent no-selection Torrent Details shell

Manual regression coverage:

1. Load the desktop dashboard with no torrent selected and verify Torrent Details is expanded with General, Trackers, Peers, HTTP sources, and Content tabs visible.
2. Verify General mirrors the normal progress/Transfer/Swarm/Information structure using em-dash values and contains no instructional empty-state message.
3. Switch through Trackers, Peers, HTTP sources, and Content with no selection; confirm structural headers/templates remain visible without fabricated torrent data.
4. Select a torrent and confirm the interim state uses animated skeleton placeholders until detail data arrives, then replaces them with live values.
5. Confirm a selected torrent with legitimately empty Peers/Trackers/HTTP sources still uses the existing meaningful empty-data copy for that selected torrent.
6. On mobile, confirm Torrent Details remains collapsed by default; manually expand it with no selection and verify the same template contract.
7. Confirm desktop viewport-proportional torrent-list sizing and General natural-fit behavior remain stable with the persistent shell present.


### Header completion notification inbox

1. On a browser with no prior bell state, load existing event history and confirm old completion events may appear in the list but do not generate a historical unread-count flood.
2. Complete a torrent and confirm the header bell badge increments after the durable completion event is recorded; open the bell and confirm the torrent name, completion label, and relative timestamp appear.
3. Opening the bell marks currently listed completion events seen: the badge clears while the entries remain visible.
4. Press **Clear** and confirm the current completion entries disappear from the bell. Open **View all notifications** and verify the same completion events remain in the durable Notifications history.
5. Complete another torrent after clearing and confirm it appears as a new unread bell entry.
6. Switch between individual clients and **All servers** and verify the bell list/badge follow the selected scope. Clearing one scope must not silently erase durable history or another browser's presentation state.
7. Reload the same browser and verify seen/cleared bell state persists; a separate browser profile should maintain independent bell presentation state.
8. Verify **View all notifications** navigates to the existing Notifications destination with detailed torrent, security, system, account, integration, and update events intact.
9. On mobile, confirm the bell popover remains above Torrent Details/bulk actions, fits the viewport without horizontal overflow, closes on outside tap/Escape, and does not displace the existing top controls.


### Add Torrent folder disclosure actions

- Open Add Torrent with a torrent containing multiple nested folders. Confirm the Expand all folders and Collapse all folders icon controls stay side-by-side and may wrap below the summary as a unit on narrow layouts.
- Before metadata is available, and for a flat torrent with no folders, both controls must remain disabled.
- Collapse several folders individually, then choose **Expand all**. Every folder and nested subfolder must become visible without changing file checkboxes or priorities.
- Choose **Collapse all**. All known folder paths must become collapsed. Expanding one parent afterward must retain the collapsed state of nested descendants until they are individually expanded or **Expand all** is used.
- After **Expand all**, the Expand control must be disabled and Collapse enabled. After **Collapse all**, Collapse must be disabled and Expand enabled.
- Switch source/magnet metadata or reset the Add Torrent form. Disclosure state must be reset for the new metadata tree and must not leak between torrents.


### Material Add Torrent folder controls

- Load a torrent containing nested folders and verify the two folder bulk actions render as adjacent Material-style icon buttons, not text controls.
- Verify the Expand all folders and Collapse all folders tooltips/accessible names match their actions.
- At desktop and narrow/mobile widths, verify the two buttons remain side-by-side; the pair may wrap below the summary text but must not stack or stretch to full width.
- Verify disabled-state transitions still follow the current tree state and that bulk disclosure does not change file checkboxes or priority values.


### Add Torrent folder control row

- Open Add Torrent with metadata containing folders and verify the Expand all folders and Collapse all folders Material buttons render side-by-side in one row.
- Verify this at desktop and narrow/mobile modal widths. The pair may move below the file summary, but the buttons must never stack vertically.
- Confirm individual folder disclosure, Expand all, Collapse all, disabled-state transitions, file selections, and file priorities are unchanged.
- The source audit rejects the old generic `.add-preview-heading div` grid selector so it cannot override the folder-action flex wrapper again.


### Mobile Add Torrent action dock

- At 820 px and below, open Add Torrent and verify the header and bottom action row stay visible while the options/metadata body scrolls independently.
- Generate metadata for a torrent with enough files/folders to make the preview substantially taller than the phone viewport; **Add torrent**, **Cancel**, and **Save .torrent file** must remain reachable without scrolling the footer into view.
- Repeat with the browser's address/navigation chrome visible and verify the action row is not clipped behind it.
- Focus a text field so the on-screen keyboard opens, then scroll the modal body and verify the action footer remains inside the visible viewport; dismiss the keyboard and verify the modal expands back to the available viewport height.
- On a device with a bottom safe area/home indicator, verify the footer has usable clearance and the primary **Add torrent** control remains a full touch target.
- Submit both a magnet-metadata add and a parsed `.torrent` add from mobile and verify the existing qBitTorrent request behavior and selected file priorities are unchanged.

### Jellyfin service integration

- Configure a Jellyfin integration with a valid URL/API key and verify Test connection reports the server version.
- Expand the saved Jellyfin integration and verify the runtime panel reports Online, the server name/version/operating system, and every configured library returned by Jellyfin.
- Verify each library shows its collection type, configured locations, and refresh status/progress when Jellyfin reports those fields. Long paths must wrap or ellipsize without horizontal overflow.
- Stop Jellyfin or use an unreachable URL and verify the runtime panel reports Offline/Libraries unavailable without exposing the API key or breaking the rest of Settings.
- Press Refresh libraries and verify Jellyfin starts a normal library scan; Torrent Dashboard must not alter library configuration or metadata options.
- Verify a successful refresh records a `jellyfin_library_refresh_requested` history event and a failed refresh records `jellyfin_library_refresh_failed`.
- Repeat at mobile width and verify the library records collapse to a single-column layout without obscuring Save/Delete/Test connection controls.

### Portable backup management

- Run `python -m unittest tests.test_backups -v` with `PYTHONPATH=src`.
- Verify created archives include configuration, history, profile assets, and notification assets while excluding `data/backups`, `data/recovery-backups`, updater staging, and release caches.
- Export a backup from one installation, import it into a second installation on the same or newer Torrent Dashboard version, restore it, and confirm the restored credentials, integrations, users, history, and assets are present.
- Confirm restore signs out the current browser session and creates a pre-restore safety backup that can be used to return to the previous state.
- Corrupt a backup payload and confirm SHA-256 validation prevents restore.


### State and input hardening

- Keep normal polling active while restoring a backup. In-flight work must finish before replacement, and new requests must authenticate against the restored state after existing sessions are invalidated.
- Confirm a settings write either finishes before restore (and is included in the safety backup) or is rejected after its old session expires; it must not silently overwrite restored state.
- Verify restore is refused while an application updater is installing or restarting.
- Run create/export/import/restore and safety-backup recovery on compiled Windows with browser sessions open. Source-level cross-install tests do not replace this operational check.
- Verify excluded data includes detached updater executables and SQLite WAL/SHM/journal sidecars. New archives must appear only after verification completes.
- Change authentication from Disabled/LAN bypass to Required, or remove a trusted network. Existing bypass sessions must lose access to both normal APIs and the recovery console.
- After an administrator resets a user's password, the user's other sessions must be invalidated.
- Test unchanged responsive detail and table behavior at widths 700, 701, 820, and 821 px: detail docking and card layout currently use separate breakpoints.

Automated regression tests cover malformed HTTP framing, byte-preserving multipart uploads, unsafe/colliding portable paths, archive limits, failed rollback reporting, parallel backup imports, connection closure, and maintenance isolation. Ordinary exception rollback is covered; interruption by process termination or power loss still requires recovery using the retained safety backup.

### Login recovery acknowledgement and backup identity isolation

- Open the sign-in screen and confirm the background gradient pulses slowly without moving the login card; with reduced-motion enabled, confirm the pulse is static.
- Complete first-run setup and confirm the recovery-key Continue button starts disabled at 10 seconds, counts down, and cannot continue before the countdown expires.
- Create a portable backup and inspect `payload/config.json`: settings, integrations, and download clients remain present, while `users`, `recovery`, and legacy authentication credential fields are absent.
- Restore both a new backup and a legacy backup that contains users, recovery data, and runtime files. Confirm the destination users and recovery key remain unchanged and destination runtime data is not replaced.

### Desktop Torrent details content-fit sizing

- Open General on desktop with a selected torrent and verify the detail pane fits its finite content without a large empty region.
- Switch to Trackers, Peers, HTTP sources, and Content and verify long content remains internally scrollable without forcing the entire page to grow unexpectedly.
- Resize through common desktop heights and confirm the torrent list yields enough space for the active detail pane while retaining the three-row minimum.

### Desktop torrent workspace scroll stability

- Record the torrent-list height, scroll the document without resizing, and allow several polling cycles; the list height must remain stable.
- Change viewport height or density and verify the list height recomputes from the new geometry.
- Confirm opening or switching detail tabs does not cause an automatic document scroll and that list/detail scrolling remain independent.

### Bottom-anchored torrent dock

- On desktop, confirm the normal Dashboard top-bar heading remains visible while the torrent list and Torrent details stay grouped below it.
- Expand and collapse Torrent details and verify the list remains independently scrollable and the document does not jump automatically.
- With no selection, verify the disclosure remains quiet; with a selection, verify the disclosure identifies the selected torrent without duplicating its identity in a second detail header.

### Fixed desktop torrent list with natural General details

- Open General on desktop and verify finite detail content uses natural height while the torrent list retains its computed viewport allocation.
- Switch to a long detail tab and confirm its body becomes internally scrollable instead of growing the whole page without bound.
- Recheck list/detail sizing after viewport and density changes.

### Torrent sort chevrons

- Sort text and numeric columns in both directions and verify each chevron stays immediately beside its label.
- Confirm numeric headings remain right-aligned, text headings remain left-aligned, and the chevron does not move to the column boundary.
