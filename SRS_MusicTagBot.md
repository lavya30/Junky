# Software Requirements Specification (SRS)

## Project: Personalized Music-Tag Discord Bot

---

## 1. Introduction

### 1.1 Purpose
This document specifies the software requirements for a Discord bot that, when a user is tagged via a slash command, replies with a music link randomly selected from a predefined list associated with that specific user.

### 1.2 Scope
The bot operates within a single Discord server (guild). Server admins/owners predefine a mapping of Discord users to a list of music links. Any member can invoke the slash command to retrieve a random song link for a tagged user. The system does not stream or play audio directly in a voice channel — it shares links only.

### 1.3 Definitions, Acronyms, Abbreviations
| Term | Meaning |
|---|---|
| SRS | Software Requirements Specification |
| Bot | The automated Discord application described here |
| Slash Command | A Discord-native command invoked with `/` |
| Guild | Discord's internal term for a server |
| Interaction | A Discord API event triggered by a user action (e.g., running a command) |

### 1.4 References
- Discord Developer Documentation — https://discord.com/developers/docs
- discord.py Library Documentation — https://discordpy.readthedocs.io

---

## 2. Overall Description

### 2.1 Product Perspective
Standalone bot application, built on `discord.py`, hosted as a persistent background process (local machine, VPS, or PaaS like Railway/Render). Integrates with the Discord API only; no external database is required for the base version (data lives in an in-memory/config mapping).

### 2.2 Product Functions
- Register and respond to a `/song` slash command.
- Accept a target user (`@mention`) as a command argument.
- Look up the target user's predefined song list.
- Randomly select and return one link from that list.
- Handle the case where a user has no predefined songs.

### 2.3 User Classes and Characteristics
| User Class | Description |
|---|---|
| Bot Owner/Admin | Defines and maintains the user→songs mapping; deploys and runs the bot |
| Server Member | Runs `/song @user` in any channel the bot can read |

### 2.4 Operating Environment
- Python 3.10+ runtime
- `discord.py` ≥ 2.3.2
- Runs on Windows/Linux/macOS or any cloud host with outbound internet access to Discord's API

### 2.5 Design and Implementation Constraints
- Requires a valid Discord Bot Token with Message Content Intent enabled.
- Bot must be invited to the target server with `applications.commands` and `bot` OAuth2 scopes.
- Song data is currently static/hardcoded (config-based), not user-editable at runtime in v1.

### 2.6 Assumptions and Dependencies
- Discord API availability and rate limits are outside this system's control.
- Music links point to external platforms (e.g., Spotify, YouTube) — the bot does not host or verify media content.
- Bot Owner manually collects Discord User IDs for mapping.

---

## 3. Specific Requirements

### 3.1 Functional Requirements

| ID | Requirement |
|---|---|
| FR1 | The system shall register a slash command `/song` with one required parameter: `user` (Discord member). |
| FR2 | On invocation, the system shall retrieve the list of songs mapped to the tagged user's Discord ID. |
| FR3 | If one or more songs exist for that user, the system shall randomly select one and reply with it in the channel. |
| FR4 | If no songs are mapped for that user, the system shall reply with a fallback message, visible only to the invoker (ephemeral). |
| FR5 | The system shall support multiple songs per user, each equally likely to be selected. |
| FR6 | The system shall sync slash commands with Discord on startup. |

### 3.2 Non-Functional Requirements

| ID | Category | Requirement |
|---|---|---|
| NFR1 | Performance | Command response shall be returned within Discord's 3-second interaction timeout. |
| NFR2 | Reliability | Bot should auto-reconnect on network drops (handled by `discord.py`'s gateway client). |
| NFR3 | Security | Bot token shall be stored in environment variables (`.env`), never hardcoded or committed to version control. |
| NFR4 | Usability | Command usage shall follow native Discord slash-command UX (autocomplete member selection). |
| NFR5 | Maintainability | User-to-song mapping shall be isolated in a single, clearly commented config block for easy updates. |

### 3.3 External Interface Requirements
- **Discord Gateway API**: real-time event handling (`on_ready`, interaction events).
- **Discord REST API**: slash command registration/sync, message responses.

---

## 4. System Feature: `/song` Command

**Description:** Returns a random music link for a tagged user.
**Trigger:** User types `/song` and selects a target member.
**Primary Flow:**
1. Invoker runs `/song user:@target`.
2. Bot receives interaction, extracts target user ID.
3. Bot looks up ID in `USER_MUSIC_MAP`.
4. Bot picks a random link from the associated list.
5. Bot responds in-channel with the link.

**Alternate Flow:** Target user ID not in map → bot sends ephemeral "no music set" message.

---

## 5. Future Enhancements (Out of Scope for v1)
- Runtime song management via additional commands (`/addsong`, `/removesong`).
- Persistent storage (database) instead of static config.
- Direct voice-channel playback instead of link sharing.
- Per-server (multi-guild) mapping support.

---

## 6. Appendix: Data Model

```python
USER_MUSIC_MAP = {
    <discord_user_id: int>: [<song_link: str>, ...],
}
```
