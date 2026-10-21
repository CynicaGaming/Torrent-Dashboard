# Torrent Dashboard Design Language

Torrent Dashboard uses a single content language across desktop and responsive surfaces. This document describes the current contract; superseded layout iterations remain in release history. These rules apply to static HTML, dynamically generated controls, dialogs, status messages, notifications, and toasts.

## Core rules

- Use **deliberate, context-aware capitalization**. Compact named destinations may read like product labels; headings, field labels, actions, status text, validation, and explanatory copy generally use sentence case. Preserve product names and established acronyms such as Torrent Dashboard, qBitTorrent, GitHub, API, IP, URL, HTTPS, and SHA-256.
- Use the **same words for the same action and outcome**. A Save action in the core Settings save bar confirms with **“Settings saved”** regardless of which Settings page is active.
- Use feature-specific success copy only when the saved object is materially different from dashboard settings, such as **“User saved”** or **“Integration saved”**.
- Use **one success channel per interaction**. Page-level saves use a toast. Scoped dialogs that already keep a visible inline status do not also emit a duplicate success toast.
- Loading and in-progress states use an active verb plus an ellipsis, for example **“Saving client settings…”** or **“Checking for updates…”**.
- Toasts are short outcome statements without terminal punctuation. Persistent inline status and explanatory copy use complete sentences with punctuation.
- Validation and errors state what the user needs to do in plain language. Avoid internal field names, implementation terminology, camelCase tokens, and title-case error sentences.
- Destructive controls use a direct verb and object, and confirmations identify what will be deleted or removed.


## Capitalization and product voice

Torrent Dashboard follows a Firefox-inspired desktop-application pattern rather than forcing one casing rule onto every surface.

- **Named destinations and compact product labels** may use title-style capitalization when they behave like stable names, for example **Access Control** and **Download Client** in setup navigation. One-word destinations such as **Dashboard**, **Settings**, and **Notifications** are naturally capitalized.
- **Page and dialog headings, field labels, buttons, menu commands, tabs, and status labels** use sentence case: **Set up your dashboard**, **Authentication mode**, **Check for updates**, **HTTP sources**, **Client settings**.
- **Explanatory copy, validation, errors, empty states, and status messages** use natural sentence case and should read as concise human language rather than implementation output.
- **Proper names, protocols, acronyms, and file or format names** retain their established form: Torrent Dashboard, qBitTorrent, GitHub, API, Web API, HTTP, IP, URL, SHA-256, `.torrent`.
- Prefer direct product concepts over legacy implementation terminology. Use **client** on client-management surfaces and **allowed IP addresses** in user-facing access controls; internal configuration keys and historical documentation do not need to be renamed solely for copy consistency.
- Prefer verb phrases for actions: **Add client**, **Test connection**, **Copy address**, **Remove torrent**. Avoid noun-heavy implementation phrases and parenthetical constructions such as **Remove torrent(s)**.
- Do not capitalize words merely because they appear in a control. Capitalization should communicate hierarchy or a proper name, not decoration.


## Iconography

Common interface symbols should use locally embedded Material-style SVG paths rather than text glyphs when an established icon exists. Disclosure chevrons, expansion controls, and file-source affordances should share this treatment so their stroke/shape quality is consistent across browsers and operating systems. Do not introduce a Google Fonts, Material Symbols font, or other remote icon dependency solely for interface chrome; Torrent Dashboard must keep these controls available offline and in self-hosted/forked deployments.

## Settings feedback contract

The core Settings pages are General, Access, Clients, Updates, and Notifications. They share the same form save bar and the same successful outcome language:

> Settings saved

Updates may use a different backend endpoint, but that implementation detail must not change the user-facing confirmation.

Integrations and Users are record-management surfaces rather than core form pages. Their successful record operations remain scoped:

- Integration saved
- Integration deleted
- User saved
- User deleted

qBitTorrent client settings and account dialogs keep their result visible inside the dialog, so they do not duplicate successful completion with a toast.

## Source and validation

New user-facing copy must be authored in its final display form. Runtime normalization must not recase deliberate authored text; `uiText()` remains only as a compatibility layer for legacy camelCase or underscore tokens and may be retired incrementally.

`release_tools/validate_ui_strings.py` enforces the high-value copy contracts that have caused drift before, including the core Settings save confirmation and known title-case legacy strings.


## Desktop legibility

Desktop layouts should use available space before shrinking text. At viewport widths of 1024 px and above:

- Primary application and table content should generally render in the **13–15 px** range.
- Supporting copy, help text, timestamps, and secondary metadata should generally stay at **11.5 px or larger**. Small badges and compact metadata labels may use approximately **10–11 px** when contrast and spacing remain strong.
- Muted text must remain visibly subordinate without becoming low-contrast. Dark and light themes both need a clear contrast step between `--text`, `--muted`, surfaces, and borders.
- Forms and navigation should gain spacing and hit area before their text is reduced. The desktop canvas should be used rather than preserving large unused margins.
- Compact density may reduce row height and spacing, but it should not restore the former undersized text baseline.
- Tablet and mobile breakpoints remain independently tuned; desktop typography rules must not simply scale responsive layouts upward.


## Docked inspectors

On desktop and tablet layouts, secondary inspection surfaces that describe a selected item should preserve access to the primary list instead of covering it.

- Torrent details dock below the torrent list as a distinct sibling panel rather than visually merging into the table surface.
- The list and inspector should each have their own border, radius, background, and clear spacing so their roles are immediately distinguishable.
- The primary torrent list remains independently scrollable while details are open.
- Torrent details are persistent and collapsible rather than closable. The collapsed state is a compact disclosure bar; it never clears the selected torrent.
- Selecting a torrent expands the inspector automatically and updates its content. With no selection, desktop starts expanded with the normal detail structure and em-dash values; mobile starts collapsed.
- The full disclosure bar is the interaction target and must remain keyboard- and touch-accessible; small icon-only collapse/close controls are not required.
- Mobile may use a bottom-sheet treatment when expanded, but the collapsed disclosure bar remains persistently reachable.

## Empty states and live dashboard metrics

Empty-state language must describe why the current surface is empty rather than using one generic message for every zero-row condition.

- A client with no torrents should say that there are no torrents yet/available; it should not imply that filters are hiding results.
- Filtered views should name the relevant condition, such as no active, completed, or paused torrents, or explain that search/filter criteria exclude all rows.
- Empty states inside bounded list workspaces should remain visually centered in the available list body and should not be pushed below a flexing scroll region.
- Primary dashboard metric cards should represent meaningful operational state. Values that refresh every polling interval, such as a per-second "Last update" timestamp, should not occupy a permanent summary card unless staleness itself requires attention.
- Connection failures and stale data should be surfaced as health/error states rather than requiring users to infer problems from a timestamp.

## Explicit update checks

Settings → Updates must not initiate a GitHub network check merely because the page is opened. Cached/local release information may render immediately, but freshness is user-directed through the Check for updates action. This keeps network activity predictable and preserves a clear distinction between viewing update settings and requesting an update check.

When the Torrent details disclosure has no selected torrent, the compact handle should remain visually quiet: show only the stable Torrent details label and disclosure affordance. Selection-specific copy appears only when a torrent is actually selected.


## Server-selection defaults

All servers is an aggregation mode, not a pseudo-client. It should be offered only when aggregation has meaning.

- With exactly one enabled download client, select that client automatically and omit All servers from the selector so client-specific commands are immediately available.
- With multiple enabled clients, expose All servers and restore the user's last valid server selection when possible.
- If a remembered client is disabled or removed, recover predictably: use All servers when multiple enabled clients remain, or the sole enabled client when only one remains.
- Server selection is a local interface preference; changing it does not modify dashboard configuration.


## Add Torrent source and content workflow

Add Torrent treats a magnet/URL and a local `.torrent` file as distinct source modes rather than presenting them as interchangeable controls in one field group.

- **Magnet link** accepts one magnet URI or HTTP(S) torrent URL and may begin metadata retrieval while the user continues configuring the torrent.
- **.torrent file** provides a dedicated drag-and-drop target that also opens the platform file picker when clicked.
- Once metadata is available, Content becomes an interactive file tree. Files can be included or excluded individually, and folder selection applies to every descendant file with mixed selections represented by an indeterminate state.
- Included files may retain qBitTorrent's Normal, High, or Maximum priority; excluded files are submitted as Do not download.
- Local `.torrent` files are parsed through qBitTorrent before add so selected file priorities can be applied through qBitTorrent's cached-metadata add path. Direct file upload remains a fallback when metadata parsing is unavailable.
- Save `.torrent` file is available for a selected local file without a network round trip. For magnet/URL metadata, Torrent Dashboard first uses qBitTorrent's metadata cache and may fall back to exporting an already-existing torrent by its canonical torrent ID.

## Hierarchical torrent content selection

Add Torrent keeps selection controls in one stable checkbox column so scanning and bulk selection remain predictable. The content column reserves one fixed disclosure slot on every row: folders use a Material disclosure icon and files use an equal-width spacer. Hierarchy indentation is applied after that shared slot, so child files visibly sit beneath their parent folder labels while Size and Priority remain aligned. Column labels describe the table directly: Name is left-aligned at the start of its column, folder rows do not repeat descendant file counts in the Priority column, and the live file/size summary makes a separate Content heading unnecessary.

For the persistent Torrent details dock, clicking the torrent whose details are already selected clears that detail context and returns the dock to its no-selection shell, expanded on desktop and collapsed on mobile. Selecting a different torrent replaces the context and expands the dock normally. The detail context must also be reconciled against each refreshed torrent list: if the selected server/hash no longer exists, clear the stale detail selection automatically and return to the same no-selection shell. The disclosure bar is the single selection-identity surface; do not repeat the torrent title/hash in a second header immediately above the detail tabs.


## Customizable torrent columns

On desktop/tablet (above the 820 px mobile breakpoint) users can rearrange and resize the torrent-list columns directly in the table header. The layout is a browser-local presentation preference, not shared configuration.

- The column set is fixed: **Name, Size, Status, Progress, Seeds, Peers, Down, Up, ETA, Ratio, Category, Tags**, which is also the default order. Columns cannot be hidden. The selection checkbox is a fixed 40 px rail on the far left and is never moved or resized; torrent commands are contextual rather than occupying a permanent Actions column.
- Default widths are proportions of the data area after the selection rail is reserved: Name 29%, Size 5%, Status 7%, Progress 20%, Seeds 4.5%, Peers 4.5%, Down 4.5%, Up 4.5%, ETA 3.5%, Ratio 4.5%, Category 6.5%, Tags 6.5%.
- **Fit-to-viewport invariant:** widths are stored as shares of the data area (not pixels) and recalculated from the live torrent viewport, so the table is always exactly viewport-width and must not introduce a horizontal scrollbar at any width or after any customization.
- **Reorder:** press a header and drag it horizontally past the 6 px movement threshold; an accent bar shows the drop position and the column (with its width) moves on release. A press that does not move is a normal click and sorts. Alt+Left/Right moves the focused header.
- **Resize:** drag the thin divider at a header's right edge. Only the grabbed boundary moves and columns to its left never change. Widening takes space from the columns to the right, nearest first, down to each column's minimum (Name 140 px, Progress 96 px, Status 72 px, others 52 px); narrowing gives the freed space to the right-hand neighbour. The last column has no divider because its right edge is the table edge. Alt+Shift+Left/Right resizes the focused header by 16 px.
- A completed drag or resize must never also trigger a sort.
- **Reset:** double-click a divider to reset widths; right-click the header for Reset column widths, Reset column order, or Reset columns.
- The layout is stored in browser-local `tdTorrentLayout` as `{order, widths}`. Invalid, partial, or unknown entries are normalized on load (missing columns return at their default position, widths are renormalized to sum to 1). The retired v0.5.95–0.5.100 `tdColumns` key is still discarded on load.
- Live one-second refreshes render cells in the saved order, so polling cannot reset or jump the layout.
- Header labels follow their body alignment. Size, Seeds, Peers, Down, Up, ETA, and Ratio are right-aligned; other data columns are left-aligned. Header clicks and keyboard activation sort, with the active direction shown by the locally embedded chevron.
- Name and other text cells use only their assigned cell width for ellipsis.
- Mobile keeps the existing card presentation in the default field order regardless of the saved desktop layout; desktop widths are cleared at the mobile breakpoint and crossing the breakpoint re-renders immediately.
- Mobile metadata rows use a consistent left-label/right-value grid. Desktop numeric-column text alignment must not leak into mobile pseudo-labels or shift labels toward the card center.
- Torrent row commands use one shared context menu: right-click opens it on pointer-based desktop interfaces, while a deliberate long press opens it on touch. Touch movement cancels the pending long press so normal vertical scrolling is not intercepted.
- On mobile, the bulk-selection overlay must clear the current Torrent details pane rather than sharing its bottom stack. Its bottom offset follows the rendered detail pane so selection actions remain fully visible in both collapsed and expanded detail states.
- Sorting remains browser-local in `tdSort`. The default may still be Added descending even though Added is not one of the visible columns.

### Compact mobile torrent cards

At the mobile breakpoint, torrent cards use a compact two-column metadata matrix rather than giving every desktop field a full-width row. Name and Progress remain full-width; Size/Status, Seeds/Peers, Download/Upload, ETA/Ratio, and Category/Tags share paired rows. The selection checkbox is positioned at the card's top-right without consuming layout height. Torrent names may wrap to at most two lines. Every metadata item keeps its label at the left of its local cell and its value at the right. This mobile layout is independent of the fixed desktop column proportions.

### Responsive torrent detail records

Trackers and Peers use purpose-built responsive detail records rather than inheriting the generic mobile table-to-card fallback. Desktop/tablet retains the normal labeled tables. At the mobile breakpoint, Peers presents the peer address as the record heading, client as secondary context, and labeled Progress, Download, and Upload metrics. Trackers presents a cleaned tracker name or URL, a human-readable status badge, labeled Seeds and Peers counts, and the tracker message only when one exists. qBitTorrent tracker status codes must not be exposed as unexplained numbers, and pseudo-trackers such as DHT, PeX, and LSD must not display literal Markdown-style asterisks. The General tab remains an independent presentation and is not altered by this responsive record treatment.

### Viewport-proportional desktop torrent workspace

The expanded desktop torrent workspace should preserve the visual balance established by the v0.5.112 layout across different monitor heights. The torrent list prefers roughly 44% of the usable viewport remaining below the workspace's stable document position, while Torrent details receives the rest. Opening details must not automatically scroll the document. The split is not a hard percentage: the rendered detail pane has priority, and the list shrinks when necessary so finite General content remains fully readable. The list height is always snapped to complete rendered torrent rows with a three-row minimum, and taller viewports may expose more than six rows instead of leaving unnecessary dead space. Document scrolling must not change the calculation; browser height and density changes may recompute it.


### Torrent sort indicator grouping

- Sortable torrent headers treat the label and chevron as one inline visual group.
- Text-oriented header groups align left; numeric header groups align right to match their body values.
- The chevron always follows the owning label with a small fixed gap rather than floating at an unrelated column edge.
- Hover, focus, and active-sort emphasis must not change the indicator's geometry.


### Persistent Torrent Details shell

- On desktop, Torrent Details remains a stable structural part of the dashboard even when no torrent is selected.
- The no-selection state renders the normal General structure and keeps Trackers, Peers, HTTP sources, and Content tabs available without explanatory empty-state copy.
- Static em-dash placeholders communicate unavailable values; animated skeletons are reserved for the brief interval after a real torrent is selected and detail data is loading.
- Mobile keeps Torrent Details collapsed by default so the persistent shell does not obscure the dashboard, but opening it exposes the same no-selection templates.
- The desktop no-selection General template participates in the existing viewport/detail fitting contract rather than introducing a second sizing model.


### Header completion notification inbox

- The application header includes a locally embedded Material-style notification bell beside the client controls and account control.
- The bell is a compact transient inbox for completed torrents, not a duplicate of the full Notifications destination. Its unread badge and recent list follow the currently selected client scope.
- Opening the bell marks the currently visible completion entries as seen while leaving them in the bell. **Clear** dismisses the bell's current completion entries only in that browser.
- Bell seen/cleared state is browser-local presentation state. Clearing the bell must never delete or mutate the durable server-side event history.
- **View all notifications** opens the main Notifications view, which remains the detailed history for torrent, security, account, update, integration, and system events.
- The popover must remain above mobile Torrent Details and bulk-action layers and must not introduce header overflow on narrow screens.


## Add Torrent folder disclosure actions

The Add Torrent content preview keeps per-folder chevrons as the primary local disclosure control and adds locally embedded **Expand all folders** / **Collapse all folders** icon controls as compact secondary actions beside the file summary. The actions operate only on the currently loaded metadata tree; they do not change file selection, priority, or torrent add options.

The controls remain disabled until the metadata contains at least one folder. **Expand all** is disabled when every folder is already open, and **Collapse all** is disabled when every known folder path is already collapsed. Bulk disclosure must preserve the same folder ordering, indentation, checkbox state, and file-priority state used by individual folder toggles.

On narrow layouts the action pair may wrap below the summary as a unit; the two icons remain side-by-side.


## Material Add Torrent folder controls

- Keep Add Torrent bulk folder disclosure visually compact: Expand all and Collapse all use locally embedded Material-style unfold icons rather than text buttons.
- The two disclosure controls are a single non-wrapping horizontal pair. On narrow layouts the pair may move as a unit, but the controls must not stack vertically or stretch into full-width actions.
- Preserve explicit accessible names and native tooltips (`Expand all folders` and `Collapse all folders`) because the visible controls are icon-only.
- Bulk disclosure remains presentation-only and must not change file selection, priority, metadata, or add-torrent options.


## Add Torrent folder control row

- The Add Torrent Expand all folders and Collapse all folders Material controls are one horizontal action pair.
- Generic Add Torrent preview-heading copy layout must not apply `display:grid` to `.add-content-folder-actions`; keep the action wrapper as the explicit non-wrapping flex row.
- The pair may move as a unit when space is constrained, but the two icon buttons must remain side-by-side.


## Mobile Add Torrent action dock

- At mobile widths, Add Torrent is sized to the browser's visual viewport rather than the larger layout viewport so browser chrome and the on-screen keyboard cannot hide the final action row.
- The Add Torrent header and footer are fixed structural regions of the modal; only the options/preview body scrolls.
- The footer respects bottom safe-area insets and keeps **Save .torrent file**, **Cancel**, and **Add torrent** continuously reachable after metadata expands the content preview.
- Mobile viewport handling is presentation-only. Metadata generation, file selection/priorities, and the qBitTorrent add request remain unchanged.

## Jellyfin service integrations

A configured service integration should expose useful operational state in the same Settings accordion where it is configured instead of requiring a separate management destination. Jellyfin shows a compact server-health summary followed by its reported libraries. Library paths may wrap on narrow layouts, while server state, library names, collection types, and scan state remain visually distinct.

Connection testing and runtime service state are separate concepts: **Test connection** validates credentials/configuration, while the Jellyfin runtime panel shows current server/library state. Destructive or metadata-editing Jellyfin actions are outside this baseline; **Refresh libraries** is an explicit administrator action and must never be triggered merely by opening Settings or by torrent completion.

## Login pulse and recovery acknowledgement

The login surface may use a slow ambient accent gradient behind the card, but the card itself stays stationary and readable. The animation uses a long ease-in-out pulse and must become static when `prefers-reduced-motion: reduce` is active.

The first-run recovery-key modal deliberately holds the Continue action for ten seconds. The button shows the remaining seconds while disabled, then returns to the normal Continue label when the acknowledgement interval ends.

## Content-fit desktop Torrent details

Desktop Torrent details should size to the finite content of the active detail tab instead of reserving a fixed-height region that creates unnecessary dead space. General may use its natural content height within the existing clamp, while long tracker, peer, HTTP-source, and content views keep their bounded internal scrolling behavior. The torrent list remains independently scrollable and yields space to the detail pane when necessary.

## Stable desktop torrent workspace height

The desktop torrent workspace derives list height from viewport size, the workspace's stable document position, rendered row height, and the active detail pane's measured need. Normal document scrolling must not change the computed list height; only meaningful layout inputs such as viewport height, density, row geometry, or active detail content should trigger recomputation.

## Client-style dashboard workspace

The Dashboard keeps normal page hierarchy in the top bar while the torrent list and persistent Torrent details inspector form one client-style workspace beneath it. The list remains the primary independently scrollable surface; Torrent details is bottom-anchored within that workspace and expands without replacing the page header or automatically scrolling the document.

## Fixed torrent list and natural-height desktop details

On desktop, the torrent list uses the computed viewport allocation while a finite General detail view may take its natural content height. The list and details retain independent sizing responsibilities so finite content does not create dead space and long detail views can continue using bounded internal scrolling.

### Torrent sort chevrons

Torrent sort chevrons remain inline with their owning label, preserve the text/numeric alignment of the column, and do not float against a column edge or alter header geometry when sort direction changes.
