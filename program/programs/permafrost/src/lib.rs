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
//!
//! v2 (discounts, 23 Nov 2026 — `claude/permafrost-discounts-plan.md`) adds the
//! LOCKED-BONUS path only: a creator-funded pool of units, paid as a bonus to
//! depositors who escrow their own units with it for 30–365 days. The liquid
//! (instant) discount is deliberately absent — it is drainable by cycling
//! (`permafrost-discount-mode.md`); the "we pay your entry costs" rebate lives
//! off-chain at the relayer instead.

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

// ------------------------------------------------------- v2: locked bonus
/// The bonus pool: a token account of the unit mint, owned by the vault PDA.
pub const POOL_SEED: &[u8] = b"pool";
/// All open positions' units (principal + bonus) sit here until claim or exit.
pub const ESCROW_SEED: &[u8] = b"escrow";
pub const POSITION_SEED: &[u8] = b"position";
/// DS3 — lock period bounds. Zero is excluded on purpose: a 0-day lock is an
/// instant bonus, which is the cycling exploit.
pub const MIN_LOCK_DAYS: u16 = 30;
pub const MAX_LOCK_DAYS: u16 = 365;
pub const DAY: i64 = 86_400;

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
            // v2 fields start zeroed (discounts off) exactly as in an upgraded v1 vault.
            v.discounts_ready = false;
            v.discount_on = false;
            v.locked_bp = 0;
            v.lock_days = 0;
            v.max_bonus = 0;
            v.next_position = 0;
            v.pool_bump = 0;
            v.escrow_bump = 0;
            v.reserved = [0u8; 232];
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

    // =============================================================== v2

    /// Create the bonus pool and the escrow (token accounts of the unit mint owned
    /// by the vault PDA). Upgrade authority only; once (the `init`s fail after).
    pub fn init_discounts(ctx: Context<InitDiscounts>) -> Result<()> {
        let v = &mut ctx.accounts.vault;
        v.discounts_ready = true;
        v.pool_bump = ctx.bumps.pool;
        v.escrow_bump = ctx.bumps.escrow;
        v.lock_days = 120; // DS3 default; discounts stay OFF until set_discount
        v.version = 2;
        emit!(DiscountsInitialized {});
        Ok(())
    }

    /// Creator dials. Upgrade authority only (DS4). No timelock: none of these can
    /// take anything from a user, and none touches an open position — each
    /// position keeps the bonus and unlock time it opened with.
    pub fn set_discount(
        ctx: Context<SetDiscount>,
        on: bool,
        locked_bp: u16,
        max_bonus: u64,
        lock_days: u16,
    ) -> Result<()> {
        require!(locked_bp as u128 <= pf::MAX_LOCKED_DISCOUNT_BP, PfErr::RateTooHigh);
        require!(
            (MIN_LOCK_DAYS..=MAX_LOCK_DAYS).contains(&lock_days),
            PfErr::LockOutOfRange
        );
        let v = &mut ctx.accounts.vault;
        require!(v.discounts_ready, PfErr::DiscountsNotReady);
        v.discount_on = on;
        v.locked_bp = locked_bp;
        v.max_bonus = max_bonus;
        v.lock_days = lock_days;
        emit!(DiscountSet { on, locked_bp, max_bonus, lock_days });
        Ok(())
    }

    /// Anyone may add units to the bonus pool, from any wallet. Bonuses are
    /// transfers out of this pool, never mints, so nobody is diluted.
    pub fn fund_pool(ctx: Context<FundPool>, amount: u64) -> Result<()> {
        require!(amount > 0, PfErr::ZeroAmount);
        let a = &ctx.accounts;
        transfer(
            &a.token_program,
            a.from.to_account_info(),
            a.unit_mint.to_account_info(),
            a.pool.to_account_info(),
            a.funder.to_account_info(),
            amount,
            UNIT_DECIMALS,
            None,
        )?;
        emit!(PoolFunded { funder: a.funder.key(), amount, pool_after: a.pool.amount + amount });
        Ok(())
    }

    /// Creator takes unused pool units to any destination it names. Reaches the
    /// pool only — escrowed units belong to open positions and cannot be touched.
    pub fn withdraw_pool(ctx: Context<WithdrawPool>, amount: u64) -> Result<()> {
        let a = &ctx.accounts;
        require!(amount > 0 && amount <= a.pool.amount, PfErr::BadAmount);
        let bump = [a.vault.vault_bump];
        let seeds: &[&[&[u8]]] = &[&[VAULT_SEED, &bump]];
        transfer(
            &a.token_program,
            a.pool.to_account_info(),
            a.unit_mint.to_account_info(),
            a.destination.to_account_info(),
            a.vault.to_account_info(),
            amount,
            UNIT_DECIMALS,
            Some(seeds),
        )?;
        emit!(PoolWithdrawn { to: a.destination.key(), amount, pool_after: a.pool.amount - amount });
        Ok(())
    }

    /// Deposit and lock. The units this deposit mints AND the bonus go into
    /// escrow for the current lock period; the depositor holds a position.
    /// `max_lock_days` and `min_bonus` protect the depositor if the creator
    /// changes the dials between signing and execution.
    pub fn deposit_locked(
        mut ctx: Context<DepositLocked>,
        max_in: [u64; 4],
        min_units_out: u64,
        max_lock_days: u16,
        min_bonus: u64,
    ) -> Result<()> {
        let a = &mut ctx.accounts;
        require!(a.vault.discount_on, PfErr::DiscountsOff);
        require!(a.vault.lock_days <= max_lock_days, PfErr::LockLongerThanAccepted);

        let holdings = [
            a.vault_leg0.amount,
            a.vault_leg1.amount,
            a.vault_leg2.amount,
            a.vault_leg3.amount,
        ];
        let supply = a.unit_mint.supply;

        // The core's own arithmetic: deposit, then the bonus with its three
        // ceilings (rate, per-deposit max, what the pool actually holds).
        let mut v = pf::Vault::new(holdings.map(u128::from), supply as u128);
        v.discount_on = true;
        v.locked_discount_bp = a.vault.locked_bp as u128;
        v.max_bonus_units = a.vault.max_bonus as u128;
        v.subsidy_units = a.pool.amount as u128;
        let out = pf::deposit(&mut v, max_in.map(u128::from)).map_err(core_err)?;
        require!(out.to_creator == 0, PfErr::UnexpectedFee);
        let bonus = pf::bonus_for(&v, out.to_depositor, v.locked_discount_bp).map_err(core_err)?;

        // The cap counts minted units only; the bonus is a transfer.
        let after = (supply as u128).checked_add(out.gross).ok_or(PfErr::Overflow)?;
        require!(after <= a.vault.cap as u128, PfErr::CapExceeded);

        let principal = u64::try_from(out.to_depositor).map_err(|_| PfErr::Overflow)?;
        let bonus = u64::try_from(bonus).map_err(|_| PfErr::Overflow)?;
        require!(principal >= min_units_out, PfErr::SlippageUnits);
        require!(bonus >= min_bonus, PfErr::BonusBelowAccepted);

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
                    to: a.escrow.to_account_info(),
                    authority: a.vault.to_account_info(),
                },
                seeds,
            ),
            principal,
        )?;
        if bonus > 0 {
            transfer(
                &a.token_program,
                a.pool.to_account_info(),
                a.unit_mint.to_account_info(),
                a.escrow.to_account_info(),
                a.vault.to_account_info(),
                bonus,
                UNIT_DECIMALS,
                Some(seeds),
            )?;
        }

        let now = Clock::get()?.unix_timestamp;
        let unlock_at = now
            .checked_add(a.vault.lock_days as i64 * DAY)
            .ok_or(PfErr::Overflow)?;
        let id = a.vault.next_position;
        let p = &mut a.position;
        p.id = id;
        p.owner = a.user.key();
        p.principal = principal;
        p.bonus = bonus;
        p.opened_at = now;
        p.unlock_at = unlock_at;
        p.bump = ctx.bumps.position;
        a.vault.next_position = id.checked_add(1).ok_or(PfErr::Overflow)?;

        emit!(LockedDeposit {
            id,
            user: a.user.key(),
            principal,
            bonus,
            taken,
            unlock_at,
            supply_after: after as u64,
        });
        Ok(())
    }

    /// At or after maturity: principal + bonus to the owner. Closes the position.
    pub fn claim_locked(ctx: Context<SettlePosition>) -> Result<()> {
        let a = &ctx.accounts;
        let now = Clock::get()?.unix_timestamp;
        require!(now >= a.position.unlock_at, PfErr::NotMatured);
        let total = a.position.principal.checked_add(a.position.bonus).ok_or(PfErr::Overflow)?;
        let bump = [a.vault.vault_bump];
        let seeds: &[&[&[u8]]] = &[&[VAULT_SEED, &bump]];
        transfer(
            &a.token_program,
            a.escrow.to_account_info(),
            a.unit_mint.to_account_info(),
            a.owner_unit.to_account_info(),
            a.vault.to_account_info(),
            total,
            UNIT_DECIMALS,
            Some(seeds),
        )?;
        emit!(PositionClaimed { id: a.position.id, owner: a.owner.key(), units: total });
        Ok(())
    }

    /// Before maturity: principal back IN FULL, bonus back to the pool. No
    /// penalty and no dial consulted. Closes the position.
    pub fn exit_locked(ctx: Context<SettlePosition>) -> Result<()> {
        let a = &ctx.accounts;
        let now = Clock::get()?.unix_timestamp;
        require!(now < a.position.unlock_at, PfErr::AlreadyMatured);
        let bump = [a.vault.vault_bump];
        let seeds: &[&[&[u8]]] = &[&[VAULT_SEED, &bump]];
        transfer(
            &a.token_program,
            a.escrow.to_account_info(),
            a.unit_mint.to_account_info(),
            a.owner_unit.to_account_info(),
            a.vault.to_account_info(),
            a.position.principal,
            UNIT_DECIMALS,
            Some(seeds),
        )?;
        if a.position.bonus > 0 {
            transfer(
                &a.token_program,
                a.escrow.to_account_info(),
                a.unit_mint.to_account_info(),
                a.pool.to_account_info(),
                a.vault.to_account_info(),
                a.position.bonus,
                UNIT_DECIMALS,
                Some(seeds),
            )?;
        }
        emit!(PositionExited {
            id: a.position.id,
            owner: a.owner.key(),
            principal: a.position.principal,
            bonus_returned: a.position.bonus,
        });
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
    // ---- v2. Carved out of v1's 256 reserved bytes (24 used), so a v1 vault
    // account decodes unchanged: every v2 field reads zero = discounts off.
    /// Pool and escrow accounts exist (set by `init_discounts`).
    pub discounts_ready: bool,
    pub discount_on: bool,
    /// Bonus as a share of the units the deposit mints, ≤ 10,000 bp (100%).
    pub locked_bp: u16,
    /// Lock period for NEW positions, MIN_LOCK_DAYS..=MAX_LOCK_DAYS.
    pub lock_days: u16,
    /// Per-deposit bonus ceiling, in unit base units. 0 disables bonuses.
    pub max_bonus: u64,
    /// Id of the next position; also the count ever opened.
    pub next_position: u64,
    pub pool_bump: u8,
    pub escrow_bump: u8,
    /// Space for fee and rebalance state in later upgrades.
    pub reserved: [u8; 232],
}

#[account]
#[derive(InitSpace)]
pub struct Position {
    pub id: u64,
    pub owner: Pubkey,
    /// The depositor's own units — returned in full on an early exit.
    pub principal: u64,
    /// Pool units — paid at maturity, returned to the pool on an early exit.
    pub bonus: u64,
    pub opened_at: i64,
    pub unlock_at: i64,
    pub bump: u8,
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

// -------------------------------------------------------- v2 accounts

#[derive(Accounts)]
pub struct InitDiscounts<'info> {
    #[account(mut)]
    pub admin: Signer<'info>,
    pub program: Program<'info, crate::program::Permafrost>,
    #[account(
        constraint = program.programdata_address()? == Some(program_data.key()) @ PfErr::Unauthorized,
        constraint = program_data.upgrade_authority_address == Some(admin.key()) @ PfErr::Unauthorized,
    )]
    pub program_data: Account<'info, ProgramData>,
    #[account(mut, seeds = [VAULT_SEED], bump = vault.vault_bump)]
    pub vault: Box<Account<'info, VaultState>>,
    #[account(seeds = [UNIT_MINT_SEED], bump = vault.unit_mint_bump)]
    pub unit_mint: Box<InterfaceAccount<'info, Mint>>,
    #[account(init, payer = admin, seeds = [POOL_SEED], bump,
        token::mint = unit_mint, token::authority = vault, token::token_program = token_program)]
    pub pool: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(init, payer = admin, seeds = [ESCROW_SEED], bump,
        token::mint = unit_mint, token::authority = vault, token::token_program = token_program)]
    pub escrow: Box<InterfaceAccount<'info, TokenAccount>>,
    pub token_program: Program<'info, Token2022>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct SetDiscount<'info> {
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

#[derive(Accounts)]
pub struct FundPool<'info> {
    pub funder: Signer<'info>,
    #[account(seeds = [VAULT_SEED], bump = vault.vault_bump, constraint = vault.discounts_ready @ PfErr::DiscountsNotReady)]
    pub vault: Box<Account<'info, VaultState>>,
    #[account(seeds = [UNIT_MINT_SEED], bump = vault.unit_mint_bump)]
    pub unit_mint: Box<InterfaceAccount<'info, Mint>>,
    #[account(mut, token::mint = unit_mint, token::token_program = token_program,
        constraint = from.owner != vault.key() @ PfErr::BadAccount)]
    pub from: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, seeds = [POOL_SEED], bump = vault.pool_bump)]
    pub pool: Box<InterfaceAccount<'info, TokenAccount>>,
    pub token_program: Program<'info, Token2022>,
}

#[derive(Accounts)]
pub struct WithdrawPool<'info> {
    pub admin: Signer<'info>,
    pub program: Program<'info, crate::program::Permafrost>,
    #[account(
        constraint = program.programdata_address()? == Some(program_data.key()) @ PfErr::Unauthorized,
        constraint = program_data.upgrade_authority_address == Some(admin.key()) @ PfErr::Unauthorized,
    )]
    pub program_data: Account<'info, ProgramData>,
    #[account(seeds = [VAULT_SEED], bump = vault.vault_bump, constraint = vault.discounts_ready @ PfErr::DiscountsNotReady)]
    pub vault: Box<Account<'info, VaultState>>,
    #[account(seeds = [UNIT_MINT_SEED], bump = vault.unit_mint_bump)]
    pub unit_mint: Box<InterfaceAccount<'info, Mint>>,
    #[account(mut, seeds = [POOL_SEED], bump = vault.pool_bump)]
    pub pool: Box<InterfaceAccount<'info, TokenAccount>>,
    /// Any unit account the creator names — except the vault's own accounts.
    #[account(mut, token::mint = unit_mint, token::token_program = token_program,
        constraint = destination.owner != vault.key() @ PfErr::BadAccount)]
    pub destination: Box<InterfaceAccount<'info, TokenAccount>>,
    pub token_program: Program<'info, Token2022>,
}

#[derive(Accounts)]
pub struct DepositLocked<'info> {
    #[account(mut)]
    pub user: Signer<'info>,

    #[account(mut, seeds = [VAULT_SEED], bump = vault.vault_bump, constraint = vault.discounts_ready @ PfErr::DiscountsNotReady)]
    pub vault: Box<Account<'info, VaultState>>,
    #[account(mut, seeds = [UNIT_MINT_SEED], bump = vault.unit_mint_bump)]
    pub unit_mint: Box<InterfaceAccount<'info, Mint>>,
    #[account(mut, seeds = [POOL_SEED], bump = vault.pool_bump)]
    pub pool: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, seeds = [ESCROW_SEED], bump = vault.escrow_bump)]
    pub escrow: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(init, payer = user, space = 8 + Position::INIT_SPACE,
        seeds = [POSITION_SEED, &vault.next_position.to_le_bytes()], bump)]
    pub position: Box<Account<'info, Position>>,

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
    pub system_program: Program<'info, System>,
}

/// Claim (at/after maturity) or exit (before). Only the position's owner signs;
/// the position closes and its rent returns to the owner.
#[derive(Accounts)]
pub struct SettlePosition<'info> {
    #[account(mut)]
    pub owner: Signer<'info>,
    #[account(seeds = [VAULT_SEED], bump = vault.vault_bump)]
    pub vault: Box<Account<'info, VaultState>>,
    #[account(seeds = [UNIT_MINT_SEED], bump = vault.unit_mint_bump)]
    pub unit_mint: Box<InterfaceAccount<'info, Mint>>,
    #[account(mut, seeds = [POOL_SEED], bump = vault.pool_bump)]
    pub pool: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, seeds = [ESCROW_SEED], bump = vault.escrow_bump)]
    pub escrow: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, close = owner, has_one = owner @ PfErr::NotPositionOwner,
        seeds = [POSITION_SEED, &position.id.to_le_bytes()], bump = position.bump)]
    pub position: Box<Account<'info, Position>>,
    #[account(mut, token::mint = unit_mint, token::token_program = token_program,
        constraint = owner_unit.owner != vault.key() @ PfErr::BadAccount)]
    pub owner_unit: Box<InterfaceAccount<'info, TokenAccount>>,
    pub token_program: Program<'info, Token2022>,
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

#[event]
pub struct DiscountsInitialized {}

#[event]
pub struct DiscountSet {
    pub on: bool,
    pub locked_bp: u16,
    pub max_bonus: u64,
    pub lock_days: u16,
}

#[event]
pub struct PoolFunded {
    pub funder: Pubkey,
    pub amount: u64,
    pub pool_after: u64,
}

#[event]
pub struct PoolWithdrawn {
    pub to: Pubkey,
    pub amount: u64,
    pub pool_after: u64,
}

#[event]
pub struct LockedDeposit {
    pub id: u64,
    pub user: Pubkey,
    pub principal: u64,
    pub bonus: u64,
    pub taken: [u64; 4],
    pub unlock_at: i64,
    pub supply_after: u64,
}

#[event]
pub struct PositionClaimed {
    pub id: u64,
    pub owner: Pubkey,
    pub units: u64,
}

#[event]
pub struct PositionExited {
    pub id: u64,
    pub owner: Pubkey,
    pub principal: u64,
    pub bonus_returned: u64,
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
    // ---- v2 (appended: v1 codes keep their numbers)
    #[msg("discounts have not been set up (init_discounts)")]
    DiscountsNotReady,
    #[msg("locked deposits are switched off")]
    DiscountsOff,
    #[msg("bonus rate above 100%")]
    RateTooHigh,
    #[msg("lock period must be 30 to 365 days")]
    LockOutOfRange,
    #[msg("the current lock period is longer than this deposit accepted")]
    LockLongerThanAccepted,
    #[msg("the bonus would be smaller than this deposit accepted")]
    BonusBelowAccepted,
    #[msg("amount must be above zero")]
    ZeroAmount,
    #[msg("amount is zero or more than the pool holds")]
    BadAmount,
    #[msg("position has not reached its unlock time")]
    NotMatured,
    #[msg("position has matured: claim it instead")]
    AlreadyMatured,
    #[msg("signer does not own this position")]
    NotPositionOwner,
}
