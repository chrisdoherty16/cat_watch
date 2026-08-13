"""ibkr_client.py — read-only IBKR pull with graceful mock fallback."""
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np
import pandas as pd
import config
try:
    from ib_async import IB
    _HAS_IB = True
except ImportError:
    IB = None
    _HAS_IB = False
try:
    import nest_asyncio
    nest_asyncio.apply()
except Exception:
    pass


@dataclass
class PortfolioSnapshot:
    connected: bool
    source: str
    account: dict = field(default_factory=dict)
    positions: pd.DataFrame = field(default_factory=pd.DataFrame)
    message: str = ""


def _mock_snapshot(reason: str) -> PortfolioSnapshot:
    positions = pd.DataFrame(
        [("RNR", 120, 228.40, 251.10), ("ACGL", 300, 92.15, 98.60),
         ("EG", 45, 372.00, 389.25), ("KBWP", 200, 98.10, 104.75),
         ("AXS", 260, 72.80, 70.15)],
        columns=["symbol", "position", "avg_cost", "mkt_price"])
    positions["mkt_value"] = positions["position"] * positions["mkt_price"]
    positions["cost_basis"] = positions["position"] * positions["avg_cost"]
    positions["unrealized_pnl"] = positions["mkt_value"] - positions["cost_basis"]
    positions["unrealized_pct"] = positions["unrealized_pnl"] / positions["cost_basis"] * 100.0
    net_liq = float(positions["mkt_value"].sum()) + 42_500.0
    account = {"NetLiquidation": net_liq, "AvailableFunds": 42_500.0,
               "UnrealizedPnL": float(positions["unrealized_pnl"].sum()),
               "RealizedPnL": 3_120.0,
               "GrossPositionValue": float(positions["mkt_value"].sum()),
               "Currency": "USD"}
    return PortfolioSnapshot(False, "MOCK", account, positions,
                             f"Showing mock portfolio — {reason}")


class IBKRClient:
    def __init__(self, settings: config.IBKRSettings = config.IBKR):
        self.s = settings

    def get_snapshot(self) -> PortfolioSnapshot:
        if not _HAS_IB:
            return _mock_snapshot("ib_async not installed")
        ib = IB()
        try:
            ib.connect(self.s.host, self.s.port, clientId=self.s.client_id,
                       timeout=self.s.timeout, readonly=self.s.readonly)
        except Exception as exc:
            return _mock_snapshot(f"could not reach TWS at {self.s.host}:{self.s.port} ({exc})")
        try:
            return PortfolioSnapshot(True, "IBKR", self._read_account(ib),
                                     self._read_positions(ib),
                                     f"Live IBKR data @ {self.s.host}:{self.s.port}")
        except Exception as exc:
            return _mock_snapshot(f"connected but data pull failed ({exc})")
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    @staticmethod
    def _read_account(ib) -> dict:
        wanted = {"NetLiquidation", "AvailableFunds", "UnrealizedPnL",
                  "RealizedPnL", "GrossPositionValue"}
        out: dict = {}
        for row in ib.accountSummary():
            if row.tag in wanted:
                try:
                    out[row.tag] = float(row.value)
                except ValueError:
                    out[row.tag] = row.value
            if row.tag == "NetLiquidation":
                out["Currency"] = row.currency
        return out

    @staticmethod
    def _read_positions(ib) -> pd.DataFrame:
        rows = []
        for item in ib.portfolio():
            c = item.contract
            qty, avg = item.position, item.averageCost
            rows.append(dict(symbol=c.symbol, position=qty, avg_cost=avg,
                             mkt_price=item.marketPrice, mkt_value=item.marketValue,
                             cost_basis=qty * avg, unrealized_pnl=item.unrealizedPNL))
        df = pd.DataFrame(rows)
        if not df.empty:
            df["unrealized_pct"] = np.where(df["cost_basis"] != 0,
                df["unrealized_pnl"] / df["cost_basis"] * 100.0, np.nan)
        return df


def load_portfolio() -> PortfolioSnapshot:
    return IBKRClient().get_snapshot()
