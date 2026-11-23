//! Permafrost v1 — seed / deposit / redeem / set_cap.
//!
//! Implements the layout approved on 22 Nov 2026 (`claude/permafrost-v1-program-layout.md`,
//! choices L1–L8). The program contains NO arithmetic of its own: every number
//! comes from `permafrost_core`, which is differential-tested against
//! `permafrost_reference.py`. This wrapper's whole job is accounts, PDAs and token
//! CPIs — which is where Solana programs actually get drained — so every account
//! below is constrained, and each constraint corresponds to a row of the layout's
//! account-validation table.
//!
//! Deliberately absent from v1 (each arrives by upgrade, or in a second vault):
//! rebalancing, the oracle, fees, the fee timelock, discount mode, transfer-hook
//! support, partial redemption.

use anchor_lang::prelude::*;
use anchor_lang::system_program;
use anchor_spl::associated_token::AssociatedToken;
use anchor_spl::token_2022::Token2022;
use anchor_spl::token_2022_extensions::token_metadata::{
    token_metadata_initialize, TokenMetadataInitialize,
};
use anchor_spl::token_interface::{self, Burn, Mint, MintTo, TokenAccount, TransferChecked};
use permafrost_core as pf;

declare_id!("48N2qchckkoWhmT9anrDU7bRt83QPLNBqj9JLZA7kEF2");

// ------------------------------------------------------------------ constants

/// L2 — the basket, hard-coded and in this order. Never configurable at set-up,
/// so set-up cannot be got wrong and no look-alike token can be slipped in.
/// Changing the basket means a new vault (decision D6).
#[cfg(not(feature = "devnet"))]
pub const LEG_MINTS: [Pubkey; 4] = [
    pubkey!("Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh"), // NVDAx
    pubkey!("XsCPL9dNWBMvFtTmwcCA5v3xWPSMEBCszbQdiLLq6aN"), // GOOGLx
    pubkey!("XspzcW1PRtgf6Wj92HCiZdjzKCyFekVD8P5Ueh3dRMX"), // MSFTx
    pubkey!("Xsa62P5mvPszXL1krVUnU5ar38bBSVcWAB6fmPCo5Zu"), // METAx
];

/// DEVNET BUILD ONLY (`--features devnet`): replicas of the four mints, created on
/// devnet by tests/devnet.py with the same extension set as the real ones
/// (tests/replica.py). A mainnet binary must never be built with this feature —
/// the launch checklist verifies the deployed binary's hash against a build
/// without it.
#[cfg(feature = "devnet")]
pub const LEG_MINTS: [Pubkey; 4] = [
    pubkey!("A15QnvzoNKst2X2aVaQANvr48kzKVRyYHzP6DXHjppFz"), // NVDAx replica
    pubkey!("5dy1EDc71EC9tsJzbbY78ZTh1vdhGMeXQNyPiUKtpGzn"), // GOOGLx replica
    pubkey!("JCs5xPjRmSBHtYKAWB6PviCAE5TUMPV4UibbKkPpRMGP"), // MSFTx replica
    pubkey!("DRcvvyhjF7chPrLbUyoX8DG67j1HYix2WKkHEaHUHHB9"), // METAx replica
];

/// L3 — the Permafrost unit has 9 decimals.
pub const UNIT_DECIMALS: u8 = 9;

/// L4 (revised 22 Nov 2026) — seed units are held by a PDA of this program that
/// the program NEVER signs for, so they can never move and supply can never
/// reach zero (design finding #1).
///
/// NOT the incinerator. The approved layout first chose the incinerator address,
/// but Token-2022 (like SPL Token) lets ANYONE burn tokens from an account whose
/// owner is the incinerator or the system program, with no signature — a
/// clean-up feature. That let a stranger burn the seed units to zero supply, and
/// let an attacker redeem them through this program and take the vault's assets.
/// Found by test A18; regression-tested by A20. Do not change this to a
/// system- or incinerator-owned address.
///
/// The one invariant this relies on: no instruction ever uses DEAD_SEED in
/// invoke_signed. Grep for it.
pub const DEAD_SEED: &[u8] = b"dead";

/// 25 units against the $25 seed, i.e. one unit ≈ $1 at launch.
pub const SEED_UNITS: u64 = 25 * 1_000_000_000;

pub const VAULT_SEED: &[u8] = b"vault";
pub const UNIT_MINT_SEED: &[u8] = b"unit_mint";

// ------------------------------------------------------------------- program

#[program]
pub mod permafrost {
    use super::*;

    /// Create the vault, the unit mint (with on-chain name/symbol), the four vault
    /// token accounts and the dead-units account (owned by the DEAD_SEED PDA). Upgrade authority only (L6).
    pub fn initialize(
        ctx: Context<Initialize>,
        cap: u64,
        name: String,
        symbol: String,
        uri: String,
    ) -> Result<()> {
        require!(
            name.len() <= 32 && symbol.len() <= 10 && uri.len() <= 200,
            PfErr::MetadataTooLong
        );

        {
            let v = &mut ctx.accounts.vault;
            v.version = 1;
            v.vault_bump = ctx.bumps.vault;
            v.unit_mint_bump = ctx.bumps.unit_mint;
            v.cap = cap;
            v.reserved = [0u8; 256];
        }

        // Token-2022 grows the mint account to hold the metadata and requires it to
        // be rent-exempt at the new size beforehand. Fund exactly the difference.
        // TLV header 4 + update authority 32 + mint 32 + three length-prefixed
        // strings + empty additional-metadata vec 4.
        let extra = 4 + 32 + 32 + (4 + name.len()) + (4 + symbol.len()) + (4 + uri.len()) + 4;
        let mint_info = ctx.accounts.unit_mint.to_account_info();
        let new_len = mint_info.data_len() + extra;
        let need = Rent::get()?
            .minimum_balance(new_len)
            .saturating_sub(mint_info.lamports());
        if need > 0 {
            system_program::transfer(
                CpiContext::new(
                    ctx.accounts.system_program.to_account_info(),
                    system_program::Transfer {
                        from: ctx.accounts.admin.to_account_info(),
                        to: mint_info.clone(),
                    },
                ),
                need,
            )?;
        }

        let bump = [ctx.bumps.vault];
        let seeds: &[&[&[u8]]] = &[&[VAULT_SEED, &bump]];
        token_metadata_initialize(
            CpiContext::new_with_signer(
                ctx.accounts.token_program.to_account_info(),
                TokenMetadataInitialize {
                    program_id: ctx.accounts.token_program.to_account_info(),
                    metadata: mint_info.clone(),
                    update_authority: ctx.accounts.vault.to_account_info(),
                    mint: mint_info.clone(),
                    mint_authority: ctx.accounts.vault.to_account_info(),
                },
                seeds,
            ),
            name,
            symbol,
            uri,
        )?;

        emit!(Initialized { cap });
        Ok(())
    }

    /// The one and only seed: the authority's four tokens into the vault, and 25
    /// units minted to the dead-units PDA. Upgrade authority only, and only once.
    pub fn seed(ctx: Context<Seed>, amounts: [u64; 4]) -> Result<()> {
        let a = &ctx.accounts;
        require!(a.unit_mint.supply == 0, PfErr::AlreadySeeded);
        // The core's own seed rules (every leg non-zero, units non-zero).
        pf::seed(amounts.map(u128::from), SEED_UNITS as u128, 0).map_err(core_err)?;

        let mints = [&a.mint0, &a.mint1, &a.mint2, &a.mint3];
        let from = [&a.admin_leg0, &a.admin_leg1, &a.admin_leg2, &a.admin_leg3];
        let to = [&a.vault_leg0, &a.vault_leg1, &a.vault_leg2, &a.vault_leg3];
        for i in 0..4 {
            transfer(
                &a.token_program,
                from[i].to_account_info(),
                mints[i].to_account_info(),
                to[i].to_account_info(),
                a.admin.to_account_info(),
                amounts[i],
                mints[i].decimals,
                None,
            )?;
        }

        let bump = [a.vault.vault_bump];
        let seeds: &[&[&[u8]]] = &[&[VAULT_SEED, &bump]];
        token_interface::mint_to(
            CpiContext::new_with_signer(
                a.token_program.to_account_info(),
                MintTo {
                    mint: a.unit_mint.to_account_info(),
                    to: a.dead_units.to_account_info(),
                    authority: a.vault.to_account_info(),
                },
                seeds,
            ),
            SEED_UNITS,
        )?;

        emit!(Seeded { amounts, units: SEED_UNITS });
        Ok(())
    }

    /// Offer up to `max_in` of each leg; receive at least `min_units_out` units or
    /// nothing happens. Only the proportional slice is taken (L5).
    pub fn deposit(ctx: Context<Deposit>, max_in: [u64; 4], min_units_out: u64) -> Result<()> {
        let a = &ctx.accounts;

        // L1 — holdings and supply are READ from the chain, never stored.
        let holdings = [
            a.vault_leg0.amount,
            a.vault_leg1.amount,
            a.vault_leg2.amount,
            a.vault_leg3.amount,
        ];
        let supply = a.unit_mint.supply;

        // A fresh in-memory core vault: fees are zero in v1 (L7). Built, used,
        // discarded — the core is never persisted.
        let mut v = pf::Vault::new(holdings.map(u128::from), supply as u128);
        let out = pf::deposit(&mut v, max_in.map(u128::from)).map_err(core_err)?;
        require!(out.to_creator == 0, PfErr::UnexpectedFee);

        // W2 — the cap. Deposits only; `redeem` never consults it.
        let after = (supply as u128)
            .checked_add(out.gross)
            .ok_or(PfErr::Overflow)?;
        require!(after <= a.vault.cap as u128, PfErr::CapExceeded);

        let units = u64::try_from(out.to_depositor).map_err(|_| PfErr::Overflow)?;
        require!(units >= min_units_out, PfErr::SlippageUnits);

        // L5 — pull exactly `required`, never more than offered.
        let mints = [&a.mint0, &a.mint1, &a.mint2, &a.mint3];
        let from = [&a.user_leg0, &a.user_leg1, &a.user_leg2, &a.user_leg3];
        let to = [&a.vault_leg0, &a.vault_leg1, &a.vault_leg2, &a.vault_leg3];
        let mut taken = [0u64; 4];
        for i in 0..4 {
            taken[i] = u64::try_from(out.required[i]).map_err(|_| PfErr::Overflow)?;
            if taken[i] > 0 {
                transfer(
                    &a.token_program,
                    from[i].to_account_info(),
                    mints[i].to_account_info(),
                    to[i].to_account_info(),
                    a.user.to_account_info(),
                    taken[i],
                    mints[i].decimals,
                    None,
                )?;
            }
        }

        let bump = [a.vault.vault_bump];
        let seeds: &[&[&[u8]]] = &[&[VAULT_SEED, &bump]];
        token_interface::mint_to(
            CpiContext::new_with_signer(
                a.token_program.to_account_info(),
                MintTo {
                    mint: a.unit_mint.to_account_info(),
                    to: a.user_unit.to_account_info(),
                    authority: a.vault.to_account_info(),
                },
                seeds,
            ),
            units,
        )?;

        emit!(Deposited {
            user: a.user.key(),
            units,
            taken,
            supply_after: after as u64,
        });
        Ok(())
    }

    /// Hand back `units_in`; receive your pro-rata share of each leg, at least
    /// `min_out` of each or nothing happens. No cap, no price, no other condition.
    /// All-or-nothing across the four legs (L8).
    pub fn redeem(ctx: Context<Redeem>, units_in: u64, min_out: [u64; 4]) -> Result<()> {
        let a = &ctx.accounts;

        let holdings = [
            a.vault_leg0.amount,
            a.vault_leg1.amount,
            a.vault_leg2.amount,
            a.vault_leg3.amount,
        ];
        let supply = a.unit_mint.supply;

        let mut v = pf::Vault::new(holdings.map(u128::from), supply as u128);
        let r = pf::redeem(&mut v, units_in as u128).map_err(core_err)?;
        require!(r.to_creator == 0, PfErr::UnexpectedFee);

        let mut paid = [0u64; 4];
        for i in 0..4 {
            paid[i] = u64::try_from(r.payouts[i]).map_err(|_| PfErr::Overflow)?;
            require!(paid[i] >= min_out[i], PfErr::SlippageLeg);
        }

        // Effects before interactions: burn first, then pay out.
        token_interface::burn(
            CpiContext::new(
                a.token_program.to_account_info(),
                Burn {
                    mint: a.unit_mint.to_account_info(),
                    from: a.user_unit.to_account_info(),
                    authority: a.user.to_account_info(),
                },
            ),
            units_in,
        )?;

        let bump = [a.vault.vault_bump];
        let seeds: &[&[&[u8]]] = &[&[VAULT_SEED, &bump]];
        let mints = [&a.mint0, &a.mint1, &a.mint2, &a.mint3];
        let from = [&a.vault_leg0, &a.vault_leg1, &a.vault_leg2, &a.vault_leg3];
        let to = [&a.user_leg0, &a.user_leg1, &a.user_leg2, &a.user_leg3];
        for i in 0..4 {
            if paid[i] > 0 {
                transfer(
                    &a.token_program,
                    from[i].to_account_info(),
                    mints[i].to_account_info(),
                    to[i].to_account_info(),
                    a.vault.to_account_info(),
                    paid[i],
                    mints[i].decimals,
                    Some(seeds),
                )?;
            }
        }

        emit!(Redeemed {
            user: a.user.key(),
            units: units_in,
            paid,
            supply_after: supply - units_in,
        });
        Ok(())
    }

    /// Change the deposit ceiling. Upgrade authority only (L6). Lowering it below
    /// the current supply traps nobody: new deposits pause, redemptions continue.
    pub fn set_cap(ctx: Context<SetCap>, new_cap: u64) -> Result<()> {
        let old = ctx.accounts.vault.cap;
        ctx.accounts.vault.cap = new_cap;
        emit!(CapSet { old, new: new_cap });
        Ok(())
    }
}

// ------------------------------------------------------------------- helpers

#[allow(clippy::too_many_arguments)]
fn transfer<'info>(
    token_program: &Program<'info, Token2022>,
    from: AccountInfo<'info>,
    mint: AccountInfo<'info>,
    to: AccountInfo<'info>,
    authority: AccountInfo<'info>,
    amount: u64,
    decimals: u8,
    signer: Option<&[&[&[u8]]]>,
) -> Result<()> {
    let accounts = TransferChecked { from, mint, to, authority };
    let ctx = match signer {
        Some(s) => CpiContext::new_with_signer(token_program.to_account_info(), accounts, s),
        None => CpiContext::new(token_program.to_account_info(), accounts),
    };
    token_interface::transfer_checked(ctx, amount, decimals)
}

fn core_err(e: pf::PfError) -> Error {
    match e {
        pf::PfError::UnseededVault => PfErr::CoreUnseeded,
        pf::PfError::EmptyLeg => PfErr::CoreEmptyLeg,
        pf::PfError::DustDeposit => PfErr::CoreDust,
        pf::PfError::BadRedeemAmount => PfErr::CoreBadRedeem,
        pf::PfError::EmptySeedLeg | pf::PfError::ZeroInitialUnits => PfErr::CoreBadSeed,
        _ => PfErr::CoreArithmetic,
    }
    .into()
}

// ------------------------------------------------------------------- state

#[account]
#[derive(InitSpace)]
pub struct VaultState {
    pub version: u8,
    pub vault_bump: u8,
    pub unit_mint_bump: u8,
    /// Max unit supply, in base units (W2). Initial value 50,000 units.
    pub cap: u64,
    /// Space for fee, rebalance and discount state in later upgrades.
    pub reserved: [u8; 256],
}

// -------------------------------------------------------------- admin check
//
// L6 — admin is whoever CURRENTLY holds the program's upgrade authority, read
// from the program's own ProgramData account. No address is hard-coded. When
// upgrade authority is burnt, `upgrade_authority_address` becomes None and no
// signer can ever match again: admin powers end with it.

#[derive(Accounts)]
pub struct Initialize<'info> {
    #[account(mut)]
    pub admin: Signer<'info>,
    pub program: Program<'info, crate::program::Permafrost>,
    #[account(
        constraint = program.programdata_address()? == Some(program_data.key()) @ PfErr::Unauthorized,
        constraint = program_data.upgrade_authority_address == Some(admin.key()) @ PfErr::Unauthorized,
    )]
    pub program_data: Account<'info, ProgramData>,

    #[account(
        init, payer = admin, space = 8 + VaultState::INIT_SPACE,
        seeds = [VAULT_SEED], bump,
    )]
    pub vault: Box<Account<'info, VaultState>>,

    // L3 — Token-2022, 9 dp, mint authority the vault PDA, metadata pointing at
    // itself. No freeze authority and no permanent delegate are created.
    #[account(
        init, payer = admin,
        seeds = [UNIT_MINT_SEED], bump,
        mint::decimals = UNIT_DECIMALS,
        mint::authority = vault,
        mint::token_program = token_program,
        extensions::metadata_pointer::authority = vault,
        extensions::metadata_pointer::metadata_address = unit_mint,
    )]
    pub unit_mint: Box<InterfaceAccount<'info, Mint>>,

    #[account(address = LEG_MINTS[0])]
    pub mint0: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[1])]
    pub mint1: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[2])]
    pub mint2: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[3])]
    pub mint3: Box<InterfaceAccount<'info, Mint>>,

    #[account(init, payer = admin, associated_token::mint = mint0,
        associated_token::authority = vault, associated_token::token_program = token_program)]
    pub vault_leg0: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(init, payer = admin, associated_token::mint = mint1,
        associated_token::authority = vault, associated_token::token_program = token_program)]
    pub vault_leg1: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(init, payer = admin, associated_token::mint = mint2,
        associated_token::authority = vault, associated_token::token_program = token_program)]
    pub vault_leg2: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(init, payer = admin, associated_token::mint = mint3,
        associated_token::authority = vault, associated_token::token_program = token_program)]
    pub vault_leg3: Box<InterfaceAccount<'info, TokenAccount>>,

    /// CHECK: the dead-units PDA (seeds [DEAD_SEED]). Owner of the dead-units
    /// account only; never a signer, never read, holds no data.
    #[account(seeds = [DEAD_SEED], bump)]
    pub dead_owner: UncheckedAccount<'info>,
    #[account(init, payer = admin, associated_token::mint = unit_mint,
        associated_token::authority = dead_owner, associated_token::token_program = token_program)]
    pub dead_units: Box<InterfaceAccount<'info, TokenAccount>>,

    pub token_program: Program<'info, Token2022>,
    pub associated_token_program: Program<'info, AssociatedToken>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct Seed<'info> {
    pub admin: Signer<'info>,
    pub program: Program<'info, crate::program::Permafrost>,
    #[account(
        constraint = program.programdata_address()? == Some(program_data.key()) @ PfErr::Unauthorized,
        constraint = program_data.upgrade_authority_address == Some(admin.key()) @ PfErr::Unauthorized,
    )]
    pub program_data: Account<'info, ProgramData>,

    #[account(seeds = [VAULT_SEED], bump = vault.vault_bump)]
    pub vault: Box<Account<'info, VaultState>>,
    #[account(mut, seeds = [UNIT_MINT_SEED], bump = vault.unit_mint_bump)]
    pub unit_mint: Box<InterfaceAccount<'info, Mint>>,

    #[account(address = LEG_MINTS[0])]
    pub mint0: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[1])]
    pub mint1: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[2])]
    pub mint2: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[3])]
    pub mint3: Box<InterfaceAccount<'info, Mint>>,

    #[account(mut, associated_token::mint = mint0, associated_token::authority = vault,
        associated_token::token_program = token_program)]
    pub vault_leg0: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint1, associated_token::authority = vault,
        associated_token::token_program = token_program)]
    pub vault_leg1: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint2, associated_token::authority = vault,
        associated_token::token_program = token_program)]
    pub vault_leg2: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint3, associated_token::authority = vault,
        associated_token::token_program = token_program)]
    pub vault_leg3: Box<InterfaceAccount<'info, TokenAccount>>,

    #[account(mut, token::mint = mint0, token::token_program = token_program,
        constraint = admin_leg0.owner != vault.key() @ PfErr::BadAccount)]
    pub admin_leg0: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, token::mint = mint1, token::token_program = token_program,
        constraint = admin_leg1.owner != vault.key() @ PfErr::BadAccount)]
    pub admin_leg1: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, token::mint = mint2, token::token_program = token_program,
        constraint = admin_leg2.owner != vault.key() @ PfErr::BadAccount)]
    pub admin_leg2: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, token::mint = mint3, token::token_program = token_program,
        constraint = admin_leg3.owner != vault.key() @ PfErr::BadAccount)]
    pub admin_leg3: Box<InterfaceAccount<'info, TokenAccount>>,

    /// CHECK: the dead-units PDA (seeds [DEAD_SEED]); never a signer.
    #[account(seeds = [DEAD_SEED], bump)]
    pub dead_owner: UncheckedAccount<'info>,
    #[account(mut, associated_token::mint = unit_mint, associated_token::authority = dead_owner,
        associated_token::token_program = token_program)]
    pub dead_units: Box<InterfaceAccount<'info, TokenAccount>>,

    pub token_program: Program<'info, Token2022>,
}

#[derive(Accounts)]
pub struct Deposit<'info> {
    pub user: Signer<'info>,

    #[account(seeds = [VAULT_SEED], bump = vault.vault_bump)]
    pub vault: Box<Account<'info, VaultState>>,
    #[account(mut, seeds = [UNIT_MINT_SEED], bump = vault.unit_mint_bump)]
    pub unit_mint: Box<InterfaceAccount<'info, Mint>>,
    #[account(mut, token::mint = unit_mint, token::token_program = token_program)]
    pub user_unit: Box<InterfaceAccount<'info, TokenAccount>>,

    #[account(address = LEG_MINTS[0])]
    pub mint0: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[1])]
    pub mint1: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[2])]
    pub mint2: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[3])]
    pub mint3: Box<InterfaceAccount<'info, Mint>>,

    #[account(mut, associated_token::mint = mint0, associated_token::authority = vault,
        associated_token::token_program = token_program)]
    pub vault_leg0: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint1, associated_token::authority = vault,
        associated_token::token_program = token_program)]
    pub vault_leg1: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint2, associated_token::authority = vault,
        associated_token::token_program = token_program)]
    pub vault_leg2: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint3, associated_token::authority = vault,
        associated_token::token_program = token_program)]
    pub vault_leg3: Box<InterfaceAccount<'info, TokenAccount>>,

    #[account(mut, token::mint = mint0, token::token_program = token_program,
        constraint = user_leg0.owner != vault.key() @ PfErr::BadAccount)]
    pub user_leg0: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, token::mint = mint1, token::token_program = token_program,
        constraint = user_leg1.owner != vault.key() @ PfErr::BadAccount)]
    pub user_leg1: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, token::mint = mint2, token::token_program = token_program,
        constraint = user_leg2.owner != vault.key() @ PfErr::BadAccount)]
    pub user_leg2: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, token::mint = mint3, token::token_program = token_program,
        constraint = user_leg3.owner != vault.key() @ PfErr::BadAccount)]
    pub user_leg3: Box<InterfaceAccount<'info, TokenAccount>>,

    pub token_program: Program<'info, Token2022>,
}

#[derive(Accounts)]
pub struct Redeem<'info> {
    pub user: Signer<'info>,

    #[account(seeds = [VAULT_SEED], bump = vault.vault_bump)]
    pub vault: Box<Account<'info, VaultState>>,
    #[account(mut, seeds = [UNIT_MINT_SEED], bump = vault.unit_mint_bump)]
    pub unit_mint: Box<InterfaceAccount<'info, Mint>>,
    #[account(mut, token::mint = unit_mint, token::token_program = token_program)]
    pub user_unit: Box<InterfaceAccount<'info, TokenAccount>>,

    #[account(address = LEG_MINTS[0])]
    pub mint0: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[1])]
    pub mint1: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[2])]
    pub mint2: Box<InterfaceAccount<'info, Mint>>,
    #[account(address = LEG_MINTS[3])]
    pub mint3: Box<InterfaceAccount<'info, Mint>>,

    #[account(mut, associated_token::mint = mint0, associated_token::authority = vault,
        associated_token::token_program = token_program)]
    pub vault_leg0: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint1, associated_token::authority = vault,
        associated_token::token_program = token_program)]
    pub vault_leg1: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint2, associated_token::authority = vault,
        associated_token::token_program = token_program)]
    pub vault_leg2: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, associated_token::mint = mint3, associated_token::authority = vault,
        associated_token::token_program = token_program)]
    pub vault_leg3: Box<InterfaceAccount<'info, TokenAccount>>,

    // Destinations: right mint, and never an account the vault owns.
    #[account(mut, token::mint = mint0, token::token_program = token_program,
        constraint = user_leg0.owner != vault.key() @ PfErr::BadAccount)]
    pub user_leg0: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, token::mint = mint1, token::token_program = token_program,
        constraint = user_leg1.owner != vault.key() @ PfErr::BadAccount)]
    pub user_leg1: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, token::mint = mint2, token::token_program = token_program,
        constraint = user_leg2.owner != vault.key() @ PfErr::BadAccount)]
    pub user_leg2: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, token::mint = mint3, token::token_program = token_program,
        constraint = user_leg3.owner != vault.key() @ PfErr::BadAccount)]
    pub user_leg3: Box<InterfaceAccount<'info, TokenAccount>>,

    pub token_program: Program<'info, Token2022>,
}

#[derive(Accounts)]
pub struct SetCap<'info> {
    pub admin: Signer<'info>,
    pub program: Program<'info, crate::program::Permafrost>,
    #[account(
        constraint = program.programdata_address()? == Some(program_data.key()) @ PfErr::Unauthorized,
        constraint = program_data.upgrade_authority_address == Some(admin.key()) @ PfErr::Unauthorized,
    )]
    pub program_data: Account<'info, ProgramData>,
    #[account(mut, seeds = [VAULT_SEED], bump = vault.vault_bump)]
    pub vault: Box<Account<'info, VaultState>>,
}

// ------------------------------------------------------------------- events

#[event]
pub struct Initialized {
    pub cap: u64,
}

#[event]
pub struct Seeded {
    pub amounts: [u64; 4],
    pub units: u64,
}

#[event]
pub struct Deposited {
    pub user: Pubkey,
    pub units: u64,
    pub taken: [u64; 4],
    pub supply_after: u64,
}

#[event]
pub struct Redeemed {
    pub user: Pubkey,
    pub units: u64,
    pub paid: [u64; 4],
    pub supply_after: u64,
}

#[event]
pub struct CapSet {
    pub old: u64,
    pub new: u64,
}

// ------------------------------------------------------------------- errors

#[error_code]
pub enum PfErr {
    #[msg("signer is not the program's current upgrade authority")]
    Unauthorized,
    #[msg("vault is already seeded")]
    AlreadySeeded,
    #[msg("deposit would take unit supply above the cap")]
    CapExceeded,
    #[msg("deposit would mint fewer units than min_units_out")]
    SlippageUnits,
    #[msg("redemption would pay less than min_out on some leg")]
    SlippageLeg,
    #[msg("a token account owned by the vault was passed where a user account belongs")]
    BadAccount,
    #[msg("metadata string too long")]
    MetadataTooLong,
    #[msg("amount does not fit in u64")]
    Overflow,
    #[msg("v1 has no fees, but the core returned a creator share")]
    UnexpectedFee,
    #[msg("core: vault is not seeded")]
    CoreUnseeded,
    #[msg("core: a leg of the vault is empty — deposits are refused (D1)")]
    CoreEmptyLeg,
    #[msg("core: deposit too small to mint a unit; nothing retained")]
    CoreDust,
    #[msg("core: redeem amount is zero or exceeds supply")]
    CoreBadRedeem,
    #[msg("core: seed amounts must all be non-zero")]
    CoreBadSeed,
    #[msg("core: arithmetic error")]
    CoreArithmetic,
}
