# Telegram Hosting Bot

Cloudflare Python Worker for managing bot-hosting requests through Telegram.

## Storage

This version uses GitHub only:

- `data/users.json`
- `data/pending.json`
- `data/hosts.json`
- `data/states.json`
- `hosts/<telegram_user_id>/<host_id>/...`

No D1 and no R2 are required.

## Required secrets

Configure these as Cloudflare Worker secrets:

- `BOT_TOKEN`
- `ADMIN_ID`
- `GITHUB_TOKEN`
- `GITHUB_OWNER`
- `GITHUB_REPO`

Optional environment variables:

- `DEFAULT_HOST_LIMIT`
- `MAX_UPLOAD_BYTES`
- `MAX_ZIP_FILES`
- `MAX_UNCOMPRESSED_BYTES`

## Important

The Worker does not execute uploaded Python code. A ZIP is first reviewed by the admin. Only after approval are its files copied into the configured GitHub repository.

The GitHub token should have only the repository permissions needed by the bot.

## Telegram webhook

Set the Telegram webhook to the deployed Worker URL.

## GitHub repository

Create the repository first. The Worker creates the `data/` JSON files automatically when needed.
