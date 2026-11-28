//! TEST ONLY. A "swap" that takes `amount_in` of one token from the caller's
//! account and pays `amount_out` of another from its own liquidity, so that
//! Permafrost's fill_leg can be exercised in LiteSVM, where Jupiter and the DEXes
//! are not available. It checks nothing a real DEX would check: the TESTS craft
//! good and malicious calls through it to prove fill_leg's guards hold whatever
//! the swap program does. Liquidity lives in token accounts owned by the
//! `pool` PDA, which tests fund directly.
use anchor_lang::prelude::*;
use anchor_spl::token_interface::{self, Mint, TokenAccount, TransferChecked};

declare_id!("E9KwSa1oVgko2b3AqLsD8zU3ppKWhth12U8tnFWNpXzG");

pub const POOL_SEED: &[u8] = b"pool";

#[program]
pub mod mockswap {
    use super::*;

    /// accounts: authority (signer), src (authority's), dst, pool_src, pool_dst,
    /// pool (PDA), src_mint, dst_mint, src_token_program, dst_token_program.
    /// Extra accounts (e.g. ones a malicious filler slips in) are ignored.
    pub fn swap(ctx: Context<Swap>, amount_in: u64, amount_out: u64) -> Result<()> {
        let a = &ctx.accounts;
        if amount_in > 0 {
            token_interface::transfer_checked(
                CpiContext::new(
                    a.src_token_program.to_account_info(),
                    TransferChecked { from: a.src.to_account_info(), mint: a.src_mint.to_account_info(), to: a.pool_src.to_account_info(), authority: a.authority.to_account_info() },
                ),
                amount_in,
                a.src_mint.decimals,
            )?;
        }
        if amount_out > 0 {
            let bump = [ctx.bumps.pool];
            let seeds: &[&[&[u8]]] = &[&[POOL_SEED, &bump]];
            token_interface::transfer_checked(
                CpiContext::new_with_signer(
                    a.dst_token_program.to_account_info(),
                    TransferChecked { from: a.pool_dst.to_account_info(), mint: a.dst_mint.to_account_info(), to: a.dst.to_account_info(), authority: a.pool.to_account_info() },
                    seeds,
                ),
                amount_out,
                a.dst_mint.decimals,
            )?;
        }
        Ok(())
    }
}

#[derive(Accounts)]
pub struct Swap<'info> {
    pub authority: Signer<'info>,
    #[account(mut)]
    pub src: InterfaceAccount<'info, TokenAccount>,
    #[account(mut)]
    pub dst: InterfaceAccount<'info, TokenAccount>,
    #[account(mut)]
    pub pool_src: InterfaceAccount<'info, TokenAccount>,
    #[account(mut)]
    pub pool_dst: InterfaceAccount<'info, TokenAccount>,
    /// CHECK: the liquidity owner PDA.
    #[account(seeds = [POOL_SEED], bump)]
    pub pool: UncheckedAccount<'info>,
    pub src_mint: InterfaceAccount<'info, Mint>,
    pub dst_mint: InterfaceAccount<'info, Mint>,
    /// CHECK: whichever token program owns src_mint.
    pub src_token_program: UncheckedAccount<'info>,
    /// CHECK: whichever token program owns dst_mint.
    pub dst_token_program: UncheckedAccount<'info>,
}
