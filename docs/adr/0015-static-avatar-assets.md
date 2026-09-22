# ADR-0015 — Message avatars use an explicit static asset origin

Status: accepted (2026-09-21)

## Context

Discord's Execute Webhook endpoint accepts `avatar_url` when a message is
created. Edit Webhook Message does not accept it, so the starter card's author
avatar is frozen at creation just like its username. Discord documents
attachments for message and embed media, not as author avatars; data URLs are
also not a documented `avatar_url` transport.

The approved visual contract is twelve 96 px Phosphor Icons avatars with one
global weight (`duotone`, `fill`, or `bold`) and one opinionated global palette:
semantic `colored`, black-on-white `black`, or white-on-black `white`. Colors
are not operator settings. The monochrome variants keep an explicit circular
background because Discord does not document a client background contract for
transparent avatar pixels. Phosphor's native duotone layer remains the same
foreground at 20% opacity, producing the secondary grey by composition.

## Decision

The repository ships the Phosphor 2.1.1 SVG sources, one local render manifest,
a deterministic renderer, and all PNG outputs under `pages/v1/`. The same
manifest and Phosphor MIT notice are copied into the published version
directory. The plugin reads only the local manifest and constructs URLs; it
never fetches remote configuration.

The official GitHub Pages workflow deploys the checked-in `pages/` directory
after it changes on `main`, once an administrator selects GitHub Actions as the
repository's Pages source. The workflow has read-only repository access and
uses the dedicated Pages and OIDC permissions; it does not commit generated
files or maintain a publication branch. With no avatar settings, the runtime
uses that official catalog.

The versioned delivery contract is:

```text
{version}/{theme}/{palette}/{size}px/{message_type}.png
```

The runtime never uploads assets, adds message attachments, embeds avatar
images, or fetches Phosphor. It only selects a URL. Unknown future event kinds
use `default`. The official theme and palette are closed choices; invalid ones
fall back to `duotone`/`colored` without stopping publication.

`avatar_base_url` instead selects a caller-owned final directory. Custom mode
appends only `{message_type}.png`: it does not impose the official version,
theme, palette, or size hierarchy and it does not fetch a remote manifest. A
custom host needs the twelve documented filenames. Invalid custom origins
disable only avatars. `avatars_enabled: false` is the explicit opt-out.

Discord does not document a control that forces consecutive webhook messages
into separate author groups. Observed clients may group equal usernames and
show only the first avatar even when later messages carry another `avatar_url`.
When avatars are active, replies therefore use the visible identity
`{profile_id} · {message_type}`; an unattributed system observation uses
`system · {message_type}`. The core still decides attribution—the transport
only adds the type needed to keep its avatar visible. With avatars disabled,
the pre-feature username behavior is unchanged. Typed identities respect
Discord's 80-character webhook-name limit by preserving the complete type and
shortening only an overlong profile id, with `…` marking the cut.

## Consequences

- Discord fetches versioned project-owned bytes rather than a mutable upstream
  icon or image-transformation service.
- Theme and palette are global, closed choices. Per-message colors remain in
  the render manifest as the reproducible approved recipe, not configuration.
- A significant visual redesign increments `v1` to `v2`; previous version
  directories stay checked in so already-published URLs remain available. This
  manual version is intentionally proportional to non-critical visual assets.
- Existing messages retain the avatar Discord fetched when they were created;
  theme or palette changes affect only later messages. This follows Discord's
  immutable author identity rather than pretending edits can repaint history.
- Reply headers are more explicit when avatars are enabled: the real profile
  id (shortened only if Discord's limit requires it) is followed by the event
  type. This is a visible compatibility tradeoff, confined to the feature that
  needs it; relying on invisible Unicode suffixes would make the workaround
  inaccessible and client-dependent.
- GitHub Pages is static project hosting, not a CDN or availability guarantee.
  The plugin cannot prove that every public asset is reachable without making
  external requests, so operators verify one path before activation.
- Zero-configuration installs ask Discord to fetch project-owned GitHub Pages
  assets. Operators who prohibit that egress disable avatars explicitly or
  provide their own public HTTPS directory.
