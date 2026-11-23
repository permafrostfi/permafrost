# DEVNET-ONLY keys — worthless, committed on purpose

These keys exist only on devnet, where SOL has no value. They are kept in the
(private) repo so a fresh build container can carry on the devnet rehearsal
instead of starting over:

- `deployer.json` — `3SmdNsrpYU8wogQtR8p534aHAUYZHEwjoXsUrxUK6Wwo`: pays for devnet
  deploys and is the mint/freeze/pause authority of the four replica mints.
- `mint_<LEG>.json` — the replica mint keypairs (only needed at creation).
- `devnet-program-48N2.json` — the devnet program id
  `48N2qchckkoWhmT9anrDU7bRt83QPLNBqj9JLZA7kEF2`.

**Never use any of these on mainnet.** The mainnet program keypair is generated
fresh at deploy time and is never committed. The admin wallet is Vishal's Phantom;
its key is never in this repo or anywhere Claude can read it.
