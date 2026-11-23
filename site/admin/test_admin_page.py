"""Drive the v3 admin page end to end against a local validator, with a stand-in Phantom.

The stand-in implements the two Phantom calls the page uses (connect,
signAndSendTransaction); it signs with a local test key. Everything else is the real
page, the real web3.js and the real (devnet build of the) program.

Setup (see claude/permafrost-resume-here.md): a local validator running mainnet's
Token-2022 + ATA programs, the four replica mints, the devnet build deployed at the
devnet program id with the admin keypair as upgrade authority, and replica legs
minted to the admin (tests/devnet.py fund, twice).

    python3 -m http.server 8077 -d site &      # from the repo root
    PF_DEVNET=1 PF_RPC=http://localhost:8899 python3 -I site/admin/test_admin_page.py ADMIN.json NEXT.json
"""

import base64
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright
from solders.keypair import Keypair

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "program" / "tests"))
import devnet as D  # noqa: E402  (RPC helpers + harness instruction builders)
import harness as H  # noqa: E402

admin = Keypair.from_bytes(bytes(json.load(open(sys.argv[1]))))
nxt = Keypair.from_bytes(bytes(json.load(open(sys.argv[2]))))
URL = "http://localhost:8077/admin/"
RPC = os.environ["PF_RPC"]

MOCK = """
window.phantom = { solana: {
  isPhantom: true,
  async connect(){ return { publicKey: { toString: () => window.__PK } }; },
  async signAndSendTransaction(tx){
    const msg = tx.serializeMessage();
    const b64 = btoa(String.fromCharCode(...msg));
    const sig = Uint8Array.from(atob(await window.pySign(b64)), c => c.charCodeAt(0));
    tx.addSignature(new solanaWeb3.PublicKey(window.__PK), sig);
    const conn = new solanaWeb3.Connection(document.getElementById('rpc').value, 'confirmed');
    const signature = await conn.sendRawTransaction(tx.serialize());
    return { signature };
  }
}};
"""


def open_page(b, kp):
    pg = b.new_page()
    pg.route("**/fonts.g*/**", lambda r: r.abort())  # cosmetic; the sandbox blocks them
    pg.expose_function("pySign", lambda m: base64.b64encode(bytes(kp.sign_message(base64.b64decode(m)))).decode())
    pg.add_init_script(f"window.__PK = '{kp.pubkey()}';" + MOCK)
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(URL)
    pg.fill("#rpc", RPC)
    pg.dispatch_event("#rpc", "change")
    pg.click("#connect")
    pg.wait_for_function(f"document.getElementById('who').textContent === '{kp.pubkey()}'")
    pg.wait_for_function("!document.getElementById('ledger').textContent.includes('Reading')", timeout=15000)
    return pg, errors


def act(pg, kind, out=None, setup=None):
    out = out or kind
    if setup:
        setup()
    pg.wait_for_function(f"!document.getElementById('do-{kind}').disabled", timeout=15000)
    pg.click(f"#do-{kind}")
    pg.wait_for_selector(f"#go-{kind}, #out-{out} .bad", timeout=20000)
    if pg.query_selector(f"#out-{out} .bad"):
        raise SystemExit(f"{kind}: {pg.inner_text(f'#out-{out}')}")
    print(f"  {kind} simulated:", pg.inner_text(f"#out-{out} .confirm p")[:150].replace("\n", " "))
    pg.click(f"#go-{kind}")
    pg.wait_for_selector(f"#out-{out} .ok, #out-{out} p.bad", timeout=40000)
    txt = pg.inner_text(f"#out-{out}")
    assert "Done" in txt, f"{kind}: {txt}"
    pg.wait_for_function("!document.getElementById('ledger').textContent.includes('Reading')", timeout=15000)


def refused(pg, kind, out, setup, expect):
    setup()
    pg.click(f"#do-{kind}")
    pg.wait_for_selector(f"#out-{out} .bad", timeout=10000)
    txt = pg.inner_text(f"#out-{out}")
    assert expect in txt, txt
    print(f"  {kind} refused before signing: {txt[:90]}")


def ledger(pg):
    return pg.inner_text("#ledger").replace("\n", " | ")


def expect(pg, text, present=True, sel="#ledger"):
    """Wait for the status ledger to (stop) showing `text` — it refreshes after each send."""
    js = f"document.querySelector({json.dumps(sel)}).innerText.includes({json.dumps(text)})"
    pg.wait_for_function(js if present else f"!({js})", timeout=15000)


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg, errors = open_page(b, admin)
        print("start:", ledger(pg)[:200])

        # ------------------------------------------------ launch (upgrade key)
        refused(pg, "init", "init", lambda: pg.fill("#init-cap", "60000"), "at most 50,000")
        pg.fill("#init-cap", "50000")
        act(pg, "init")
        half = [x // 2 for x in [2 * a for a in H.SEED_AMOUNTS]]
        for i, v in enumerate(half):
            pg.fill(f"#seed-{i}", f"{v / 1e8:.8f}")
        act(pg, "seed")
        act(pg, "v3")
        expect(pg, "Fees in force")
        assert "this is you" in pg.inner_text("#ledger").split("Admin key")[1][:120].lower(), "admin = upgrade key's wallet"

        # ------------------------------------------------ the five powers
        act(pg, "cap", setup=lambda: pg.fill("#cap-new", "40000"))
        expect(pg, "cap 40,000 units")
        refused(pg, "disc", "disc", lambda: pg.fill("#disc-days", "20"), "30 to 365")
        def disc():
            pg.select_option("#disc-on", "1")
            pg.fill("#disc-rate", "10"); pg.fill("#disc-max", "5"); pg.fill("#disc-days", "120")
        act(pg, "disc", setup=disc)
        expect(pg, "10% bonus"); expect(pg, "120-day lock")

        # The admin gets PAIF the ordinary way (a deposit), then tops up the pool from the page.
        amts = [x // 2 for x in [2 * a for a in H.SEED_AMOUNTS]]
        D.send([D.W.create_ata_ix(admin.pubkey(), admin.pubkey(), H.UNIT_MINT), D.W.ix_deposit(admin, amts)],
               [admin], label="admin deposits to get PAIF")
        pg.click("#refresh")
        pg.wait_for_function("!document.getElementById('do-fund').disabled", timeout=15000)
        act(pg, "fund", setup=lambda: pg.fill("#pool-in", "10"))
        expect(pg, "10 units free")
        refused(pg, "pool", "pool", lambda: pg.fill("#pool-out", "11"), "holds only 10")
        def wd():
            pg.fill("#pool-out", "4"); pg.fill("#pool-to", str(nxt.pubkey()))
        act(pg, "pool", setup=wd)
        expect(pg, "6 units free")
        assert D.units_of(nxt.pubkey()) == 4 * H.UNIT, "withdrawal reached the named wallet"

        refused(pg, "fees", "fees", lambda: pg.fill("#fee-entry", "1.5"), "capped at 1%")
        def fees():
            pg.fill("#fee-entry", "0.3"); pg.fill("#fee-exit", "0.5"); pg.fill("#fee-stream", "0.75")
        act(pg, "fees", setup=fees)
        expect(pg, "Fee change pending"); expect(pg, "entry 0.3%")
        def cut():
            pg.fill("#fee-entry", "0"); pg.fill("#fee-exit", "0"); pg.fill("#fee-stream", "0")
        act(pg, "fees", setup=cut)
        expect(pg, "Fee change pending", present=False)  # a lower setting cancels the pending rise

        act(pg, "propose", out="admin", setup=lambda: pg.fill("#admin-new", str(nxt.pubkey())))
        expect(pg, "proposed successor")
        assert pg.is_disabled("#do-accept"), "the current admin can't accept on the successor's behalf"
        print("admin page:", ledger(pg))
        pg.screenshot(path="/tmp/admin_v3.png", full_page=True)
        assert not errors, errors

        # ------------------------------------------------ the successor accepts
        pg2, errors2 = open_page(b, nxt)
        assert "you can accept" in pg2.inner_text("#st-admin").lower()
        act(pg2, "accept", out="admin")
        expect(pg2, "proposed successor", present=False)
        assert "this is you" in pg2.inner_text("#ledger").split("Admin key")[1][:120].lower()
        assert pg2.is_disabled("#do-init") and pg2.is_disabled("#do-upg"), "the admin key is not the upgrade key"
        pg.click("#refresh")
        pg.wait_for_function("document.getElementById('st-cap').textContent === 'admin key only'", timeout=15000)
        assert not errors2, errors2
        print("handover: done; the old wallet now shows every admin power as 'admin key only'")
        b.close()
        print("ALL PASSED")


main()
