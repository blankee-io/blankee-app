# Blankee push relay

The one piece of Blankee that is not self-hosted, and why.

Apple delivers a push to an app only from a key tied to the account that
signed the app. The iOS app is signed by blankee.io, so the key is ours, and a
self-hosted server cannot have it: whoever holds the key can push anything to
every phone running the app. This relay holds it instead, and forwards a nudge
on a server's behalf. It knows as little as it can:

- The phone registers its own device token here, with a secret it made up.
  Only the secret's hash is kept.
- A server asks for a push with that token, the same secret (the phone gave it
  to the server too), and the id of the notification. If the secret matches,
  Apple is told to wake the phone with that id and nothing else.
- The phone's notification service extension fetches the notification from the
  person's own server, using its own credentials, and fills in the alert before
  it is shown.

No text, amount or server address passes through here. Without both a phone's
token and its secret nobody can push to it, and one phone is limited to
`RELAY_HOURLY_LIMIT` pushes an hour, whoever asks.

## API

`POST /v1/register` `{token, secret, environment}` - from the app. `environment`
is `sandbox` (a build from Xcode or TestFlight) or `production` (App Store).
Re-registering a token replaces its secret, which is what a reinstalled app
needs. Answers 204.

`POST /v1/push` `{token, secret, id}` - from a Blankee server. Answers
`{sent: true}`; `401` for a wrong secret; `404` for a token never registered;
`410` when Apple says the device is gone (the relay forgets it, and the server
should too); `429` past the hourly limit; `502` when Apple cannot be reached.

`GET /v1/health` - `{ok, apns}`.

## Running it

`sudo ./install.sh` on a Debian/Ubuntu host: code in `/opt/blankee-relay`,
settings in `/etc/blankee-relay/relay.env`, the SQLite database in
`/var/lib/blankee-relay`, a systemd service on `127.0.0.1:8100`. Fill in the
four `APNS_` values, put the `.p8` beside them, restart, and point the reverse
proxy that holds the TLS certificate for `push.blankee.io` at the port.

A Blankee server uses the relay when `PUSH_RELAY_URL` is set (the installer's
default is `https://push.blankee.io`) and no `APNS_` settings of its own are.
An installation that signs its own build sets the `APNS_` values instead and
never talks to the relay.
