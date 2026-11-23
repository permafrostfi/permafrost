"""Drive the admin page end to end against a local validator, with a stand-in Phantom.

The stand-in implements the two Phantom calls the page uses (connect,
signAndSendTransaction); it signs with a local test key that is the program's
upgrade authority on the local chain. Everything else is the real page, the real
web3.js and the real program.

    python3 -m http.server 8077 -d site &      # from the repo root
    python3 -I site/admin/test_admin_page.py /path/to/admin-keypair.json [buffer-address]
"""

import base64
import json
import sys

from playwright.sync_api import sync_playwright
from solders.keypair import Keypair

kp = Keypair.from_bytes(bytes(json.load(open(sys.argv[1]))))
URL = "http://localhost:8077/admin/"

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


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.route("**/fonts.g*/**", lambda r: r.abort())  # cosmetic; the sandbox blocks them
        pg.expose_function("pySign", lambda m: base64.b64encode(bytes(kp.sign_message(base64.b64decode(m)))).decode())
        pg.add_init_script(f"window.__PK = '{kp.pubkey()}';" + MOCK)
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(URL)
        pg.fill("#rpc", "http://localhost:8899")
        pg.dispatch_event("#rpc", "change")
        pg.click("#connect")
        pg.wait_for_selector("text=this is you", timeout=15000)
        print("status:", pg.inner_text("#ledger").replace("\n", " | "))

        def act(kind, setup=None):
            if setup:
                setup()
            pg.click(f"#do-{kind}")
            pg.wait_for_selector(f"#go-{kind}, #out-{kind} .bad", timeout=20000)
            if pg.query_selector(f"#out-{kind} .bad"):
                raise SystemExit(f"{kind}: {pg.inner_text(f'#out-{kind}')}")
            print(f"{kind} simulated:", pg.inner_text(f"#out-{kind} .confirm p")[:160])
            pg.click(f"#go-{kind}")
            pg.wait_for_selector(f"#out-{kind} .ok, #out-{kind} .bad", timeout=40000)
            txt = pg.inner_text(f"#out-{kind}")
            assert "Done" in txt, f"{kind}: {txt}"
            print(f"{kind}: done")

        # A wrong input is refused before anything is built.
        pg.fill("#init-cap", "60000")
        pg.click("#do-init")
        assert "at most 50,000" in pg.inner_text("#out-init"), pg.inner_text("#out-init")
        pg.fill("#init-cap", "50000")

        act("init")
        pg.wait_for_function("document.getElementById('st-seed').textContent === 'ready'", timeout=15000)
        pg.click("#seed-max")
        act("seed")
        pg.wait_for_function("document.getElementById('st-seed').textContent === 'done'", timeout=15000)
        act("cap", lambda: pg.fill("#cap-new", "40000"))
        pg.wait_for_selector("text=cap 40,000 units", timeout=15000)
        print("final:", pg.inner_text("#ledger").replace("\n", " | "))
        print("legs:", pg.inner_text("#legs tbody").replace("\n", " | ").replace("\t", " "))
        if len(sys.argv) > 2:  # optional: a buffer whose authority is the admin key
            act("upg", lambda: pg.fill("#upg-buffer", sys.argv[2]))
        # A second set-up is not offered once the vault exists.
        assert pg.is_disabled("#do-init")
        pg.screenshot(path="/tmp/admin_page.png", full_page=True)
        assert not errors, errors
        print("page errors: none")
        b.close()


main()
