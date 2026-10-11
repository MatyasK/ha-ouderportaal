# Security

## Reporting a problem

Please do not open a public issue for a security problem.
Report it privately instead, with [GitHub's private vulnerability reporting](https://github.com/MatyasK/ha-ouderportaal/security/advisories/new).

Describe what can go wrong and how to make it happen.
Leave out your own password, tokens and your children's data: a made-up example is enough.

This is a hobby project, so there is no fixed response time, but security problems come first.

## Supported versions

Only the latest release gets fixes.

## What counts

The integration keeps your portal password in Home Assistant, and your children's day rhythm, photos and messages.
These are security problems:

- The password, or a token from the portal, ends up somewhere it should not, such as a log, the diagnostics, an event or an entity's attributes.
- Personal data, such as names, photos or messages, ends up in the diagnostics.
- Saved photos or messages can be reached without logging in to Home Assistant.
- Saving a photo writes outside `media/ouderportaal/`.

A problem in the portal itself is for Konnect, not for this project.

<!-- generated with AI -->
