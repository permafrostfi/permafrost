//! v4 (28 Nov 2026) — ORDERS: invest with a stablecoin, withdraw to a stablecoin,
//! one signature each (`claude/permafrost-v4-usdc-orders-plan.md`).
//!
//! The user signs ONE small transaction. Everything after that is done by a
//! "filler" (normally the gas service, but anyone may fill) under rules this
//! program enforces, so the filler can delay an order but never take from it or
//! give a worse price than the user signed for:
//!
//!   invest:   order_invest ──▶ fill_leg ×4 (Jupiter) ──▶ settle_invest
//!   withdraw: order_redeem ──▶ fill_leg ×4 (Jupiter) ──▶ settle_redeem
//!   either:   cancel_order (owner any time; anyone after expiry) — full refund
//!
//! What keeps the escrow safe (each is a test in tests/test_v4.py):
//!   1. The escrow is a PDA of this program. Its five token accounts (one
//!      stablecoin, four legs) are its associated token accounts, so every
//!      instruction here can check it is looking at the real ones.
//!   2. The ONLY program the escrow ever signs a CPI for is Jupiter (address
//!      fixed in code, like the basket). It never signs a bare token transfer
//!      except the ones written here: into the vault at settle, back to the
//!      owner at cancel, both to constrained addresses.
//!   3. Around the Jupiter call, ALL FIVE escrow balances are snapshotted. After
//!      it: the input may fall by at most the user's budget for that leg, the
//!      output must rise by at least the user's minimum for that leg, and the
//!      other three must not fall at all. A route that quietly sells another
//!      leg, or sends the output elsewhere, is refused.
//!   4. Settle re-runs the vault's own deposit arithmetic on what was actually
//!      bought and refuses unless it mints at least the units the user signed.
//!   5. After `expires_at`, anyone can cancel and everything in the escrow goes
//!      back to the owner. The site does this for stalled orders.
//!   6. The vault's own holdings never appear in a Jupiter call with a signer:
//!      fill_leg signs with the ORDER's seeds only. Grep: no VAULT_SEED here
//!      outside order_redeem's payout and settle's deposit.

use anchor_lang::prelude::*;
use anchor_lang::solana_program::{instruction::Instruction, program::invoke_signed};
use anchor_spl::associated_token::AssociatedToken;
use anchor_spl::token::Token;
use anchor_spl::token_2022::Token2022;
use anchor_spl::token_interface::{self, Mint, TokenAccount};

use crate::{fee_account_address, PfErr, VaultState, LEG_MINTS, UNIT_MINT_SEED, VAULT_SEED};

pub const ORDER_SEED: &[u8] = b"order";

/// The only program a fill may call. Jupiter v6's aggregator, mainnet.
#[cfg(not(feature = "mockswap"))]
pub const SWAP_PROGRAM: Pubkey = pubkey!("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4");
/// TEST BUILD ONLY (`--features mockswap`): a stand-in swap program for LiteSVM
/// (programs/mockswap). A mainnet binary must never be built with this feature.
#[cfg(feature = "mockswap")]
pub const SWAP_PROGRAM: Pubkey = pubkey!("E9KwSa1oVgko2b3AqLsD8zU3ppKWhth12U8tnFWNpXzG");

/// The stablecoins an order may be paid in or out of (decided 25 Nov: USDC, USDT).
pub const STABLE_MINTS: [Pubkey; 2] = [
    pubkey!("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"), // USDC
    pubkey!("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"), // USDT
];

/// An order must be fillable for at least a minute and expires within an hour.
pub const MIN_ORDER_LIFE: i64 = 60;
pub const MAX_ORDER_LIFE: i64 = 3600;

pub const KIND_INVEST: u8 = 0;
pub const KIND_REDEEM: u8 = 1;
pub const ALL_LEGS: u8 = 0b1111;

#[account]
#[derive(InitSpace)]
pub struct Order {
    pub kind: u8,
    pub bump: u8,
    /// Bitmask of legs filled (bit i = leg i).
    pub filled: u8,
    pub owner: Pubkey,
    /// Paid the rent of this account and its five token accounts; gets it back.
    pub payer: Pubkey,
    pub stable_mint: Pubkey,
    pub id: u64,
    /// invest: stablecoin escrowed. redeem: units handed back.
    pub amount_in: u64,
    /// The most the settler may keep as its fee, in stablecoin base units.
    pub max_fee: u64,
    /// invest: the fewest units the owner accepts. redeem: unused (0).
    pub min_units: u64,
    /// Per leg. invest: stock the fill must deliver, at least. redeem: stablecoin
    /// the sale must bring, at least.
    pub min_out: [u64; 4],
    /// Per leg. invest: stablecoin the fill may spend, at most. redeem: the stock
    /// held for sale (its whole balance).
    pub budget: [u64; 4],
    pub created_at: i64,
    pub expires_at: i64,
}

pub fn is_stable(m: &Pubkey) -> bool {
    STABLE_MINTS.contains(m)
}

// ------------------------------------------------------------------ accounts

#[derive(Accounts)]
#[instruction(id: u64)]
pub struct OrderInvest<'info> {
    pub user: Signer<'info>,
    /// Pays the rent of the order and its token accounts (the gas service).
    #[account(mut)]
    pub payer: Signer<'info>,
    #[account(init, payer = payer, space = 8 + Order::INIT_SPACE,
        seeds = [ORDER_SEED, user.key().as_ref(), &id.to_le_bytes()], bump)]
    pub order: Box<Account<'info, Order>>,

    #[account(constraint = is_stable(&stable_mint.key()) @ PfErr::BadStableMint)]
    pub stable_mint: Box<InterfaceAccount<'info, Mint>>,
    #[account(mut, token::mint = stable_mint, token::authority = user, token::token_program = stable_token_program)]
    pub user_stable: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(init, payer = payer, associated_token::mint = stable_mint, associated_token::authority = order,
        associated_token::token_program = stable_token_program)]
    pub order_stable: Box<InterfaceAccount<'info, TokenAccount>>,

    #[account(address = LEG_MINTS[0])]
    pub mint0: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[1])]
    pub mint1: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[2])]
    pub mint2: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[3])]
    pub mint3: Box<InterfaceAccount<'info, Mint>>,
    #[account(init, payer = payer, associated_token::mint = mint0, associated_token::authority = order,
        associated_token::token_program = token_program)]
    pub order_leg0: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(init, payer = payer, associated_token::mint = mint1, associated_token::authority = order,
        associated_token::token_program = token_program)]
    pub order_leg1: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(init, payer = payer, associated_token::mint = mint2, associated_token::authority = order,
        associated_token::token_program = token_program)]
    pub order_leg2: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(init, payer = payer, associated_token::mint = mint3, associated_token::authority = order,
        associated_token::token_program = token_program)]
    pub order_leg3: Box<InterfaceAccount<'info, TokenAccount>>,

    pub stable_token_program: Program<'info, Token>,
    pub token_program: Program<'info, Token2022>,
    pub associated_token_program: Program<'info, AssociatedToken>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
#[instruction(id: u64)]
pub struct OrderRedeem<'info> {
    pub user: Signer<'info>,
    #[account(mut)]
    pub payer: Signer<'info>,
    #[account(init, payer = payer, space = 8 + Order::INIT_SPACE,
        seeds = [ORDER_SEED, user.key().as_ref(), &id.to_le_bytes()], bump)]
    pub order: Box<Account<'info, Order>>,

    #[account(mut, seeds = [VAULT_SEED], bump = vault.vault_bump)]
    pub vault: Box<Account<'info, VaultState>>,
    #[account(mut, seeds = [UNIT_MINT_SEED], bump = vault.unit_mint_bump)]
    pub unit_mint: Box<InterfaceAccount<'info, Mint>>,
    #[account(mut, token::mint = unit_mint, token::authority = user, token::token_program = token_program)]
    pub user_unit: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, address = fee_account_address(&unit_mint.key()) @ PfErr::BadFeeAccount)]
    pub fee_account: Box<InterfaceAccount<'info, TokenAccount>>,

    #[account(constraint = is_stable(&stable_mint.key()) @ PfErr::BadStableMint)]
    pub stable_mint: Box<InterfaceAccount<'info, Mint>>,
    #[account(init, payer = payer, associated_token::mint = stable_mint, associated_token::authority = order,
        associated_token::token_program = stable_token_program)]
    pub order_stable: Box<InterfaceAccount<'info, TokenAccount>>,

    #[account(address = LEG_MINTS[0])]
    pub mint0: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[1])]
    pub mint1: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[2])]
    pub mint2: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[3])]
    pub mint3: Box<InterfaceAccount<'info, Mint>>,
    #[account(mut, associated_token::mint = mint0, associated_token::authority = vault, associated_token::token_program = token_program)]
    pub vault_leg0: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint1, associated_token::authority = vault, associated_token::token_program = token_program)]
    pub vault_leg1: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint2, associated_token::authority = vault, associated_token::token_program = token_program)]
    pub vault_leg2: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint3, associated_token::authority = vault, associated_token::token_program = token_program)]
    pub vault_leg3: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(init, payer = payer, associated_token::mint = mint0, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg0: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(init, payer = payer, associated_token::mint = mint1, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg1: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(init, payer = payer, associated_token::mint = mint2, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg2: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(init, payer = payer, associated_token::mint = mint3, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg3: Box<InterfaceAccount<'info, TokenAccount>>,

    pub stable_token_program: Program<'info, Token>,
    pub token_program: Program<'info, Token2022>,
    pub associated_token_program: Program<'info, AssociatedToken>,
    pub system_program: Program<'info, System>,
}

/// One Jupiter swap for one leg of an open order. Anyone may call; the escrow's
/// five accounts are named so their balances can be checked around the call.
/// Jupiter's own accounts come as remaining accounts and are forwarded as given.
#[derive(Accounts)]
pub struct FillLeg<'info> {
    pub filler: Signer<'info>,
    #[account(mut, seeds = [ORDER_SEED, order.owner.as_ref(), &order.id.to_le_bytes()], bump = order.bump)]
    pub order: Box<Account<'info, Order>>,
    #[account(address = order.stable_mint)]
    pub stable_mint: Box<InterfaceAccount<'info, Mint>>,
    #[account(mut, associated_token::mint = stable_mint, associated_token::authority = order, associated_token::token_program = stable_token_program)]
    pub order_stable: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(address = LEG_MINTS[0])]
    pub mint0: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[1])]
    pub mint1: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[2])]
    pub mint2: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[3])]
    pub mint3: Box<InterfaceAccount<'info, Mint>>,
    #[account(mut, associated_token::mint = mint0, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg0: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint1, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg1: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint2, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg2: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint3, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg3: Box<InterfaceAccount<'info, TokenAccount>>,
    /// CHECK: the one program the escrow may sign for; its address is fixed above.
    #[account(address = SWAP_PROGRAM @ PfErr::NotSwapProgram)]
    pub swap_program: UncheckedAccount<'info>,
    pub stable_token_program: Program<'info, Token>,
    pub token_program: Program<'info, Token2022>,
}

#[derive(Accounts)]
pub struct SettleInvest<'info> {
    #[account(mut)]
    pub settler: Signer<'info>,
    #[account(mut, close = payer, has_one = owner, has_one = payer,
        seeds = [ORDER_SEED, order.owner.as_ref(), &order.id.to_le_bytes()], bump = order.bump,
        constraint = order.kind == KIND_INVEST @ PfErr::BadOrderKind)]
    pub order: Box<Account<'info, Order>>,
    /// CHECK: the order's owner, by has_one. Receives the units and the change.
    pub owner: UncheckedAccount<'info>,
    /// CHECK: whoever paid the rent, by has_one. Receives it back.
    #[account(mut)]
    pub payer: UncheckedAccount<'info>,

    #[account(mut, seeds = [VAULT_SEED], bump = vault.vault_bump)]
    pub vault: Box<Account<'info, VaultState>>,
    #[account(mut, seeds = [UNIT_MINT_SEED], bump = vault.unit_mint_bump)]
    pub unit_mint: Box<InterfaceAccount<'info, Mint>>,
    /// CHECK: the owner's associated PAIF account; created here if missing.
    #[account(mut)]
    pub user_unit: UncheckedAccount<'info>,
    #[account(mut, address = fee_account_address(&unit_mint.key()) @ PfErr::BadFeeAccount)]
    pub fee_account: Box<InterfaceAccount<'info, TokenAccount>>,

    #[account(address = order.stable_mint)]
    pub stable_mint: Box<InterfaceAccount<'info, Mint>>,
    #[account(mut, associated_token::mint = stable_mint, associated_token::authority = order, associated_token::token_program = stable_token_program)]
    pub order_stable: Box<InterfaceAccount<'info, TokenAccount>>,
    /// CHECK: the owner's associated stablecoin account; created here if missing.
    #[account(mut)]
    pub user_stable: UncheckedAccount<'info>,
    /// Where the settler's fee goes: any account of the order's stablecoin.
    #[account(mut, token::mint = stable_mint, token::token_program = stable_token_program)]
    pub fee_to: Box<InterfaceAccount<'info, TokenAccount>>,

    #[account(address = LEG_MINTS[0])]
    pub mint0: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[1])]
    pub mint1: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[2])]
    pub mint2: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[3])]
    pub mint3: Box<InterfaceAccount<'info, Mint>>,
    #[account(mut, associated_token::mint = mint0, associated_token::authority = vault, associated_token::token_program = token_program)]
    pub vault_leg0: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint1, associated_token::authority = vault, associated_token::token_program = token_program)]
    pub vault_leg1: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint2, associated_token::authority = vault, associated_token::token_program = token_program)]
    pub vault_leg2: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint3, associated_token::authority = vault, associated_token::token_program = token_program)]
    pub vault_leg3: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint0, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg0: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint1, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg1: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint2, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg2: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint3, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg3: Box<InterfaceAccount<'info, TokenAccount>>,

    pub stable_token_program: Program<'info, Token>,
    pub token_program: Program<'info, Token2022>,
    pub associated_token_program: Program<'info, AssociatedToken>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct SettleRedeem<'info> {
    #[account(mut)]
    pub settler: Signer<'info>,
    #[account(mut, close = payer, has_one = owner, has_one = payer,
        seeds = [ORDER_SEED, order.owner.as_ref(), &order.id.to_le_bytes()], bump = order.bump,
        constraint = order.kind == KIND_REDEEM @ PfErr::BadOrderKind)]
    pub order: Box<Account<'info, Order>>,
    /// CHECK: by has_one.
    pub owner: UncheckedAccount<'info>,
    /// CHECK: by has_one.
    #[account(mut)]
    pub payer: UncheckedAccount<'info>,
    #[account(seeds = [VAULT_SEED], bump = vault.vault_bump)]
    pub vault: Box<Account<'info, VaultState>>,

    #[account(address = order.stable_mint)]
    pub stable_mint: Box<InterfaceAccount<'info, Mint>>,
    #[account(mut, associated_token::mint = stable_mint, associated_token::authority = order, associated_token::token_program = stable_token_program)]
    pub order_stable: Box<InterfaceAccount<'info, TokenAccount>>,
    /// CHECK: the owner's associated stablecoin account; created here if missing.
    #[account(mut)]
    pub user_stable: UncheckedAccount<'info>,
    #[account(mut, token::mint = stable_mint, token::token_program = stable_token_program)]
    pub fee_to: Box<InterfaceAccount<'info, TokenAccount>>,

    #[account(address = LEG_MINTS[0])]
    pub mint0: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[1])]
    pub mint1: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[2])]
    pub mint2: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[3])]
    pub mint3: Box<InterfaceAccount<'info, Mint>>,
    // Sale dust (should be zero) goes to the vault, never to the settler.
    #[account(mut, associated_token::mint = mint0, associated_token::authority = vault, associated_token::token_program = token_program)]
    pub vault_leg0: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint1, associated_token::authority = vault, associated_token::token_program = token_program)]
    pub vault_leg1: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint2, associated_token::authority = vault, associated_token::token_program = token_program)]
    pub vault_leg2: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint3, associated_token::authority = vault, associated_token::token_program = token_program)]
    pub vault_leg3: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint0, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg0: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint1, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg1: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint2, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg2: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint3, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg3: Box<InterfaceAccount<'info, TokenAccount>>,

    pub stable_token_program: Program<'info, Token>,
    pub token_program: Program<'info, Token2022>,
    pub associated_token_program: Program<'info, AssociatedToken>,
    pub system_program: Program<'info, System>,
}

/// Refund: everything the escrow holds goes back to the owner. The owner may
/// cancel at any time; anyone may after expiry (the site does, for stalled orders).
#[derive(Accounts)]
pub struct CancelOrder<'info> {
    #[account(mut)]
    pub canceller: Signer<'info>,
    #[account(mut, close = payer, has_one = owner, has_one = payer,
        seeds = [ORDER_SEED, order.owner.as_ref(), &order.id.to_le_bytes()], bump = order.bump)]
    pub order: Box<Account<'info, Order>>,
    /// CHECK: by has_one.
    pub owner: UncheckedAccount<'info>,
    /// CHECK: by has_one.
    #[account(mut)]
    pub payer: UncheckedAccount<'info>,

    #[account(address = order.stable_mint)]
    pub stable_mint: Box<InterfaceAccount<'info, Mint>>,
    #[account(mut, associated_token::mint = stable_mint, associated_token::authority = order, associated_token::token_program = stable_token_program)]
    pub order_stable: Box<InterfaceAccount<'info, TokenAccount>>,
    /// CHECK: the owner's associated stablecoin account; created if needed.
    #[account(mut)]
    pub user_stable: UncheckedAccount<'info>,

    #[account(address = LEG_MINTS[0])]
    pub mint0: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[1])]
    pub mint1: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[2])]
    pub mint2: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[3])]
    pub mint3: Box<InterfaceAccount<'info, Mint>>,
    #[account(mut, associated_token::mint = mint0, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg0: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint1, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg1: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint2, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg2: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint3, associated_token::authority = order, associated_token::token_program = token_program)]
    pub order_leg3: Box<InterfaceAccount<'info, TokenAccount>>,
    /// CHECK: the owner's associated accounts for the legs; created only for a
    /// leg the escrow holds some of.
    #[account(mut)]
    pub user_leg0: UncheckedAccount<'info>,
    /// CHECK: as above.
    #[account(mut)]
    pub user_leg1: UncheckedAccount<'info>,
    /// CHECK: as above.
    #[account(mut)]
    pub user_leg2: UncheckedAccount<'info>,
    /// CHECK: as above.
    #[account(mut)]
    pub user_leg3: UncheckedAccount<'info>,

    pub stable_token_program: Program<'info, Token>,
    pub token_program: Program<'info, Token2022>,
    pub associated_token_program: Program<'info, AssociatedToken>,
    pub system_program: Program<'info, System>,
}

// ------------------------------------------------------------------- helpers

/// transfer_checked through whichever token program owns the mint.
#[allow(clippy::too_many_arguments)]
pub fn xfer<'info>(
    token_program: AccountInfo<'info>,
    from: AccountInfo<'info>,
    mint: AccountInfo<'info>,
    to: AccountInfo<'info>,
    authority: AccountInfo<'info>,
    amount: u64,
    decimals: u8,
    signer: Option<&[&[&[u8]]]>,
) -> Result<()> {
    if amount == 0 {
        return Ok(());
    }
    let accounts = token_interface::TransferChecked { from, mint, to, authority };
    match signer {
        Some(s) => token_interface::transfer_checked(CpiContext::new_with_signer(token_program, accounts, s), amount, decimals),
        None => token_interface::transfer_checked(CpiContext::new(token_program, accounts), amount, decimals),
    }
}

/// Close one of the escrow's token accounts (must be empty); rent to `to`.
pub fn close_escrow_account<'info>(
    token_program: AccountInfo<'info>,
    account: AccountInfo<'info>,
    to: AccountInfo<'info>,
    order: AccountInfo<'info>,
    seeds: &[&[&[u8]]],
) -> Result<()> {
    token_interface::close_account(CpiContext::new_with_signer(
        token_program,
        token_interface::CloseAccount { account, destination: to, authority: order },
        seeds,
    ))
}

/// The owner's associated token account, created if it does not exist. Returns
/// nothing: callers transfer into it by AccountInfo.
#[allow(clippy::too_many_arguments)]
pub fn ensure_ata<'info>(
    associated_token_program: AccountInfo<'info>,
    payer: AccountInfo<'info>,
    ata: AccountInfo<'info>,
    owner: AccountInfo<'info>,
    mint: AccountInfo<'info>,
    system_program: AccountInfo<'info>,
    token_program: AccountInfo<'info>,
) -> Result<()> {
    // The address must be the real associated account, or a transfer into it
    // could go to an attacker's account passed in its place.
    let expected = anchor_spl::associated_token::get_associated_token_address_with_program_id(owner.key, mint.key, token_program.key);
    require_keys_eq!(*ata.key, expected, PfErr::BadAccount);
    anchor_spl::associated_token::create_idempotent(CpiContext::new(
        associated_token_program,
        anchor_spl::associated_token::Create { payer, associated_token: ata, authority: owner, mint, system_program, token_program },
    ))
}

/// Forward a swap to the swap program, with the ORDER as the only added signer,
/// then check all five escrow balances. `in_ix`/`out_ix` are indices 0..=4 into
/// [stable, leg0, leg1, leg2, leg3].
#[allow(clippy::too_many_arguments)]
pub fn guarded_swap<'info>(
    swap_program: &AccountInfo<'info>,
    remaining: &[AccountInfo<'info>],
    data: Vec<u8>,
    order_key: Pubkey,
    seeds: &[&[&[u8]]],
    escrow: &mut [&mut Box<InterfaceAccount<'info, TokenAccount>>; 5],
    in_ix: usize,
    out_ix: usize,
    max_spend: u64,
    min_receive: u64,
) -> Result<(u64, u64)> {
    let before: Vec<u64> = escrow.iter().map(|a| a.amount).collect();
    let metas: Vec<AccountMeta> = remaining
        .iter()
        .map(|a| AccountMeta {
            pubkey: *a.key,
            is_signer: a.is_signer || *a.key == order_key,
            is_writable: a.is_writable,
        })
        .collect();
    // The escrow signs for nobody else: every other signer flag comes from the
    // transaction itself, and this program's own accounts are not passed.
    for m in &metas {
        require!(m.pubkey != crate::ID, PfErr::BadAccount);
    }
    let ix = Instruction { program_id: *swap_program.key, accounts: metas, data };
    let mut infos: Vec<AccountInfo<'info>> = remaining.to_vec();
    infos.push(swap_program.clone());
    invoke_signed(&ix, &infos, seeds)?;

    let mut spent = 0u64;
    let mut got = 0u64;
    for (k, acc) in escrow.iter_mut().enumerate() {
        acc.reload()?;
        let after = acc.amount;
        if k == in_ix {
            spent = before[k].saturating_sub(after);
            require!(spent <= max_spend, PfErr::BudgetExceeded);
        } else if k == out_ix {
            got = after.checked_sub(before[k]).ok_or(PfErr::FillBelowMinimum)?;
            require!(got >= min_receive, PfErr::FillBelowMinimum);
        } else {
            require!(after >= before[k], PfErr::OrderDrained);
        }
    }
    Ok((spent, got))
}
