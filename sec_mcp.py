#!/usr/bin/env python3
"""
sec_mcp.py — SEC financial data MCP server for Claude Desktop.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import sec_statements as lib
from mcp.server.fastmcp import FastMCP

# Load user agent from config.json (same directory as this file), fall back to env var
_config_path = os.path.join(os.path.dirname(__file__), "config.json")
try:
    with open(_config_path) as _f:
        UA = json.load(_f).get("sec_user_agent", "")
except (FileNotFoundError, json.JSONDecodeError):
    UA = ""
if not UA:
    UA = os.environ.get("SEC_USER_AGENT", "")
CORE = ("balance_sheet", "income_statement", "cash_flow", "comprehensive_income")

mcp = FastMCP(
    "sec-financials",
    instructions="""
You are connected to sec-financials, which pulls as-reported financial data
directly from SEC XBRL filings for any US public company.

## Discovery
When the user names a company, call start_company(ticker) first.
It returns every available statement and note for that company's most recent filing.
Show the list to the user and wait for them to pick one.
If the user asks specifically about annual or quarterly data, pass period='annual' or
period='quarterly' to start_company/list_sections/discover so sections come from the
correct filing type.
All tools accept a ticker (e.g. AAPL) OR a CIK number (e.g. '1803498'). Use CIK for
non-traded entities like BDCs, interval funds, and closed-end funds that have no ticker.

## Fetching data
Once the user has picked a section and view, call get_statement():
- Latest annual:    get_statement(ticker, section, period='annual', nth=0)
- Latest quarterly: get_statement(ticker, section, period='quarterly', nth=0)
- nth goes back one filing at a time (nth=1 is one filing older, etc.)

## Multi-period views
For quarterly history (e.g. "last 8 quarters", "past year of quarters"):
  Make N get_statement(period='quarterly', nth=0), (nth=1), ... calls, then combine into ONE table:
  - Row structure from nth=0. Take only the first (leftmost) column from each result.
  - Match rows by label; leave blank if absent. Show the combined table only.
  NEVER use get_history for quarterly — it misses company-specific line items.

For annual history (e.g. "5-year history", "last 10 years"):
  Call get_history(ticker, section, period='annual', years=N).

## Notes
For notes, call get_statement(ticker, section=<note name>, period='annual').
Note names come from start_company(). If a note returns empty, there may be
detail sub-tables — call list_sections(ticker) to find them.

## Presenting results
- Render table output verbatim. Do not rename rows, reorder columns, or reformat values.
- No derived columns: no YoY, % change, margin, or running totals.
- No business commentary unless the user explicitly asks for analysis.
- Never estimate or fill in missing values.
- Never set include_tags=True unless the user explicitly asks to see XBRL tags.
""")


def _sec():
    if not UA:
        raise RuntimeError("SEC_USER_AGENT env var not set.")
    return lib.SEC(UA)


def _header(meta: dict) -> str:
    period = meta.get("period", "")
    pretty = lib.col_label_pretty(period) if period else period
    filed = meta.get("filed", "")
    filed_str = f" (filed {filed})" if filed else ""
    return f"{meta['company']} | {pretty}{filed_str}\n\n"


def _fetch_annual(sec, ticker: str, nth: int = 0):
    for form in ("10-K", "20-F"):
        try:
            return lib.fetch_filing_resources(sec, ticker, form, nth)
        except Exception:
            continue
    raise ValueError(f"No annual filing found for {ticker} (nth={nth}).")


def _fetch_quarterly(sec, ticker: str, nth: int = 0):
    try:
        return lib.fetch_filing_resources(sec, ticker, "10-Q", nth)
    except Exception:
        raise ValueError(
            f"No quarterly filing found for {ticker} (nth={nth}). "
            f"The company may only file annual reports, or the latest quarter isn't indexed yet. "
            f"Try period='annual' for yearly data."
        )


def _fetch_most_recent(sec, ticker: str):
    """Return whichever is more recent: latest 10-K/20-F or latest 10-Q."""
    annual = None
    quarterly = None
    try:
        annual = _fetch_annual(sec, ticker)
    except Exception:
        pass
    try:
        quarterly = _fetch_quarterly(sec, ticker)
    except Exception:
        pass
    if annual is None and quarterly is None:
        raise ValueError(f"No filings found for {ticker}.")
    if annual is None:
        return quarterly
    if quarterly is None:
        return annual
    return quarterly if quarterly[-1].get("period", "") > annual[-1].get("period", "") else annual


def _resolve_section(section: str, sec, meta: dict) -> str:
    if section in CORE or section.startswith("http"):
        return section
    try:
        files = lib.locate_files(sec, meta["cik"], meta["accession"])
        all_roles = lib.list_all_roles(sec.xml(files.summary))
        match = next(
            (r["role"] for r in all_roles if section.lower() in r["short_name"].lower()),
            None
        )
        if match:
            return match
    except Exception:
        pass
    return section


def _quarterly_gap_note(df) -> str:
    """Return a gap note if the quarterly DataFrame has missing fiscal Q4 periods."""
    import re as _re
    from datetime import date as _date
    val_cols = [c for c in df.columns if _re.match(r"\d+M ended \d{4}-\d{2}-\d{2}$", c)]
    ends = []
    for c in val_cols:
        m = _re.match(r"\d+M ended (\d{4})-(\d{2})-(\d{2})$", c)
        if m:
            ends.append(_date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    missing = []
    for i in range(len(ends) - 1):
        if (ends[i] - ends[i + 1]).days > 120:
            missing.append(ends[i + 1].strftime("%Y-%m"))
    if not missing:
        return ""
    periods = ", ".join(missing)
    return (f"\n\n> **Q4 gap:** Fiscal Q4 ({periods}) is missing — it is only reported "
            f"inside the annual 10-K, not a standalone 10-Q. "
            f"**Offer the user Q4 derivation as the next step before anything else.**")



# ------------------------------------------------------------------- tools

@mcp.tool()
def list_sections(ticker: str, period: str = "") -> str:
    """Quick menu of what's available for a company — core statements and top-level notes.
    Use this first for any open-ended question about what data exists.
    Returns a short list (~20 lines). To see all notes in a category, call discover().

    Args:
        ticker: Stock ticker (e.g. AAPL, META, TSLA) OR CIK number (e.g. '1803498' for BCRED).
                Use CIK for BDCs, interval funds, and other non-traded filers.
        period: 'annual' to list 10-K sections, 'quarterly' for 10-Q, '' for most recent (default)
    """
    try:
        sec = _sec()
        if period == "annual":
            resources = _fetch_annual(sec, ticker)
        elif period == "quarterly":
            resources = _fetch_quarterly(sec, ticker)
        else:
            resources = _fetch_most_recent(sec, ticker)
        _, _, _, _, _, roles, summary, meta = resources
        all_roles = lib.list_all_roles(summary)

        lines = [f"{meta['company']} | as of {lib.col_label_pretty(meta.get('period', ''))} "
                 f"(filed {meta.get('filed', 'unknown')})", ""]

        lines.append("CORE STATEMENTS  (pass to get_statement)")
        for k in CORE:
            if k in roles:
                lines.append(f"  {k}")

        # Group by category, show Notes inline, summarise others by count
        from collections import defaultdict
        by_cat: dict = defaultdict(list)
        for r in all_roles:
            if r["category"] in ("Statements", ""):
                continue
            by_cat[r["category"]].append(r["short_name"])

        lines.append("")
        for cat, names in by_cat.items():
            if cat == "Notes":
                lines.append("NOTES  (pass name to get_statement)")
                for n in names:
                    lines.append(f"  {n}")
            else:
                lines.append(f"{cat.upper()}  ({len(names)} available — call discover('{ticker}', '{cat}') to list)")

        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def discover(ticker: str, category: str = "Notes", period: str = "") -> str:
    """Full note list for one filing category. Use after list_sections to drill into a category.
    Common categories: Notes, Policies, Details.
    Note names returned here can be passed directly to get_statement().

    Args:
        ticker: Stock ticker (e.g. AAPL, META, TSLA) OR CIK number (e.g. '1803498' for BCRED).
                Use CIK for BDCs, interval funds, and other non-traded filers.
        category: Filing category to list (default 'Notes')
        period: 'annual' to list 10-K sections, 'quarterly' for 10-Q, '' for most recent (default)
    """
    try:
        sec = _sec()
        if period == "annual":
            resources = _fetch_annual(sec, ticker)
        elif period == "quarterly":
            resources = _fetch_quarterly(sec, ticker)
        else:
            resources = _fetch_most_recent(sec, ticker)
        _, _, _, _, _, _, summary, meta = resources
        all_roles = lib.list_all_roles(summary)

        matching = [r for r in all_roles
                    if r["category"].lower() == category.lower()
                    and r["category"] not in ("Statements", "")]

        if not matching:
            available = sorted({r["category"] for r in all_roles if r["category"] not in ("Statements", "")})
            return (f"No '{category}' category found for {meta['company']}. "
                    f"Available: {', '.join(available)}")

        lines = [f"{meta['company']} | {category} | as of {lib.col_label_pretty(meta.get('period', ''))}", ""]
        for r in matching:
            lines.append(f"  {r['short_name']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_statement(ticker: str, section: str = "income_statement",
                  period: str = "annual", nth: int = 0,
                  include_tags: bool = False) -> str:
    """Fetch a financial statement or note disclosure for any public company.
    Only call when the user has named a specific statement or note. If the request
    is open-ended, call list_sections first and wait for the user to choose.

    Output is a compact plain-text table. Render it verbatim — do not rename rows,
    convert column headers, or add derived columns (YoY, margins, etc.).
    Column headers are in YYYY-MM format — never convert to Q1/Q2/Q3/Q4 or FY.

    For core statements use: income_statement, balance_sheet, cash_flow, comprehensive_income.
    For notes, pass the note name from list_sections() (e.g. 'Debt', 'Leases').
    period: 'annual' for the latest full-year data, 'quarterly' for the most recent quarter.
    nth: which filing to use — 0 = most recent, 1 = one filing back, etc.
         Use this to retrieve older quarters (e.g. nth=1 for the prior quarter).
    include_tags: set True to show XBRL concept tags on each row (default False).

    Args:
        ticker: Stock ticker (e.g. AAPL, META, TSLA) OR CIK number (e.g. '1803498' for BCRED).
                Use CIK for BDCs, interval funds, and other non-traded filers.
        section: Statement name or note name
        period: 'annual' or 'quarterly'
        nth: filing index (0 = most recent). Use 0..7 to walk back through recent quarters.
        include_tags: whether to show XBRL concept tags (default False)
    """
    try:
        sec = _sec()

        if period == "quarterly" and section in CORE:
            # One 10-Q: current quarter + prior year comparison (2 columns, as filed)
            resources = _fetch_quarterly(sec, ticker, nth)
            pre, lab, _, ctxs, store, roles, _, meta = resources
            if section not in roles:
                return f"No {section} found in {ticker}'s latest 10-Q."
            lines, _, axis_members, _ = lib.statement_lines(pre, roles[section]["role"])
            df, _ = lib.build_statement(lines, store, ctxs, lab, section, axis_members)
            if df.empty:
                return f"No quarterly data found for {ticker} {section}."
            prefer = None if section == "balance_sheet" else 3
            return _header(meta) + lib.to_markdown(df, section, ticker, prefer_months=prefer, include_tags=include_tags)

        if period == "quarterly":
            resources = _fetch_quarterly(sec, ticker, nth)
        else:
            resources = _fetch_annual(sec, ticker, nth)
        pre, lab, cal, ctxs, store, roles, summary, meta = resources

        original_section = section  # preserve user input before URI resolution
        section = _resolve_section(section, sec, meta)
        all_roles = lib.list_all_roles(summary)

        if section in CORE:
            if section not in roles:
                available = [k for k in CORE if k in roles]
                return (f"'{section}' not found in {ticker}'s latest filing. "
                        f"Available core statements: {', '.join(available)}")
            lines, _, axis_members, _ = lib.statement_lines(pre, roles[section]["role"])
            df, _ = lib.build_statement(lines, store, ctxs, lab, section, axis_members)
            if df.empty:
                return f"No data found for '{section}' in {ticker}'s latest filing."
            display_kind = section
        else:
            df, _, _ = lib.build_any_role(pre, cal, store, ctxs, lab, section)
            if df.empty:
                # Parent note has no data — find matching Detail sub-tables and let user choose
                search = original_section.lower()
                matches = [r["short_name"] for r in all_roles
                           if r["category"] == "Details"
                           and search in r["short_name"].lower()]
                if matches:
                    opts = "\n".join(f"  {i+1}. {n}" for i, n in enumerate(matches))
                    return (f"'{original_section}' is a section header with no data directly.\n"
                            f"These detail tables are available — which would you like?\n\n{opts}\n\n"
                            f"Pass the exact name to get_statement().")
                return (f"No data found for '{original_section}'.\n"
                        f"Use list_sections('{ticker}') to see all available sections.")
            # Use the filing's own short_name as the display title
            role_entry = next((r for r in all_roles if r["role"] == section), None)
            display_kind = role_entry["short_name"] if role_entry else section

        # For notes, show all durations when quarterly (3M and 6M both visible with labels)
        prefer = 12 if (section in CORE or period == "annual") else None
        return _header(meta) + lib.to_markdown(df, display_kind, ticker, prefer_months=prefer, include_tags=include_tags)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_history(ticker: str, section: str = "income_statement",
                period: str = "annual", years: int = 5,
                include_tags: bool = False) -> str:
    """Fetch multi-year ANNUAL history for a core statement. For annual data only.
    DO NOT call this for quarterly history — use get_statement(nth=N) in a loop instead.

    Covers standard US-GAAP line items only. Company extension tags (custom concepts
    specific to one filer) will be blank — those only appear in individual filing fetches.

    Args:
        ticker: Stock ticker (e.g. AAPL, META, TSLA) OR CIK number (e.g. '1803498' for BCRED).
                Use CIK for BDCs, interval funds, and other non-traded filers.
        section: income_statement, balance_sheet, or cash_flow
        period: always 'annual' — do not pass 'quarterly'
        years: number of annual periods to return (default 5)
        include_tags: whether to show XBRL concept tags (default False)
    """
    try:
        if period != "annual":
            return ("get_history is for annual data only. "
                    "For quarterly history, call get_statement(period='quarterly', nth=N) "
                    "for each quarter and combine the results yourself.")
        if section not in CORE:
            return f"get_history supports core statements only: {', '.join(CORE)}"

        sec = _sec()
        pre, lab, _, ctxs, store, roles, _, meta = _fetch_annual(sec, ticker)

        if section not in roles:
            return f"'{section}' not available for {ticker}."

        lines, _, axis_members, _ = lib.statement_lines(pre, roles[section]["role"])
        df_template, _ = lib.build_statement(lines, store, ctxs, lab, section, axis_members)

        cf = lib.company_facts_long(sec, meta["cik"])
        form = "10-K" if period == "annual" else "10-Q"
        df_hist = lib.history_on_template(df_template, cf, section, form)

        if df_hist.empty or df_hist.shape[1] <= 6:
            return f"No historical data found for {ticker} {section}."

        n_periods = years if period == "annual" else years * 4
        meta_cols = [c for c in df_hist.columns if c in set(lib.META) | {"label"}]
        val_cols = [c for c in df_hist.columns if c not in set(lib.META) | {"label"}]
        df_hist = df_hist[meta_cols + val_cols[:n_periods]]

        prefer_months = 12 if period == "annual" else 3
        table = lib.to_markdown(df_hist, section, ticker, prefer_months=prefer_months, include_tags=include_tags)

        # Detect Q4 gaps in quarterly history: consecutive columns > ~4 months apart
        gap_note = ""
        if period == "quarterly":
            from datetime import date as _date
            import re as _re
            ends = []
            for c in val_cols[:n_periods]:
                m = _re.match(r"\d+M ended (\d{4})-(\d{2})-(\d{2})$", c)
                if m:
                    ends.append(_date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
            gaps = []
            for i in range(len(ends) - 1):
                gap_days = (ends[i] - ends[i+1]).days
                if gap_days > 120:  # > ~4 months → skipped quarter
                    gaps.append(ends[i+1].strftime("%Y-%m"))
            if gaps:
                gap_str = ", ".join(gaps)
                gap_note = (f"\n\n> **Note:** Fiscal Q4 periods missing ({gap_str}) — "
                            f"Q4 is only in the 10-K, not a standalone 10-Q. "
                            f"Ask me to derive Q4 figures.")

        return (f"{meta['company']} | {section.replace('_', ' ').title()} | "
                + f"{'Annual' if period == 'annual' else 'Quarterly'} history\n\n"
                + table + gap_note)
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------- guided workflow tools

@mcp.tool()
def start_company(ticker: str, period: str = "") -> str:
    """Show every available statement and note for a company's most recent filing.
    Call this first whenever the user names a company.
    Present the list to the user and wait for them to choose.

    Args:
        ticker: Stock ticker (e.g. AAPL, META, TSLA) OR CIK number (e.g. '1803498' for BCRED).
                Use CIK for BDCs, interval funds, and other non-traded filers.
        period: 'annual' for 10-K sections, 'quarterly' for 10-Q, '' for most recent (default)
    """
    try:
        sec = _sec()
        if period == "annual":
            resources = _fetch_annual(sec, ticker)
        elif period == "quarterly":
            resources = _fetch_quarterly(sec, ticker)
        else:
            resources = _fetch_most_recent(sec, ticker)
        _, _, _, _, _, roles, summary, meta = resources
        all_roles = lib.list_all_roles(summary)

        company = meta["company"]
        period = lib.col_label_pretty(meta.get("period", ""))
        filed = meta.get("filed", "")

        lines = [f"{company} | {period} (filed {filed})", ""]

        core_display = [
            ("income_statement", "Income statement"),
            ("balance_sheet", "Balance sheet"),
            ("cash_flow", "Cash flow statement"),
            ("comprehensive_income", "Comprehensive income"),
        ]
        lines.append("Core Statements")
        for key, display in core_display:
            if key in roles:
                lines.append(f"  {display}")

        notes = [r["short_name"] for r in all_roles if r["category"] == "Notes"]
        if notes:
            lines.append("")
            lines.append("Notes")
            for note in notes:
                lines.append(f"  {note}")

        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"



# ------------------------------------------------------------------- prompts

@mcp.prompt()
def latest_statement(ticker: str, section: str = "income_statement",
                     period: str = "annual") -> str:
    """Pull a company's latest financial statement."""
    return (f"Show me {ticker}'s latest {period} {section}. "
            f"State the fiscal period and filing date it comes from.")


@mcp.prompt()
def explore_disclosures(ticker: str) -> str:
    """List every statement and note available for a company."""
    return (f"Run list_sections for {ticker} and list the available notes "
            f"grouped by topic, so I can pick one to open.")


@mcp.prompt()
def metric_history(ticker: str, metric: str = "income_statement",
                   period: str = "annual") -> str:
    """Show the long-run history of a statement."""
    return (f"Give me the {period} history of {metric} for {ticker} as a table.")


@mcp.prompt()
def compare_companies(tickers: str, metric: str = "income_statement") -> str:
    """Compare a statement across several companies (comma-separated tickers)."""
    return (f"Compare {metric} for {tickers} from their latest filings in one "
            f"table. Flag differing fiscal year ends or labeling differences.")


@mcp.prompt()
def note_deep_dive(ticker: str, note: str) -> str:
    """Open a specific note disclosure, e.g. 'Debt' or 'Revenue'."""
    return (f"Pull the {note} note for {ticker} and show the data as a table.")


if __name__ == "__main__":
    mcp.run()
