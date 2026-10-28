"""Hypothetical limit fills; no native queue/execution certainty is implied."""

from dataclasses import replace

from .types import FillAllocation, FillModel, SimOrder, TradePrint
from .execution_eligibility import classify_execution, execution_status


class PrintLiquidity:
    """One shared ledger per model/replay, including both brokers/accounts.

    Invoke once per order/print pair, in the declared simulated order priority.
    Shared consumption prevents two account episodes spending the same volume.
    Separate model ledgers are required: models are alternative worlds.
    """

    def __init__(self, model: FillModel) -> None:
        self.model = model
        self._prints: dict[tuple[str, str], tuple[int, int]] = {}
        self._pairs: set[tuple[str, tuple[str, str]]] = set()
        self._filled: dict[str, int] = {}
        self._queue: dict[str, int] = {}
        self._asks: dict[tuple[str, str], tuple[object, int, int]] = {}
        self._placements: set[str] = set()
        self._unknown: dict[str, tuple[str, tuple[int, int], int | None, object]] = {}

    def register_planning_uncertainty(self, owner_id, symbol, at, end_ns, evidence):
        if evidence.eligible is not None or evidence.raw_id != at.raw_id or evidence.raw_source != at.source:
            raise ValueError("source_bound_planning_unknown_required")
        if end_ns is not None and end_ns <= at.event_ns:
            raise ValueError("planning_unknown_window_required")
        self._unknown[f"planning:{owner_id}:{at.raw_id}"] = (
            symbol, (at.event_ns, at.sequence), end_ns, evidence,
        )

    def unknown_evidence(self, symbol: str, event_ns: int, sequence: int):
        return tuple(evidence for sym, start, end, evidence in self._unknown.values()
                     if sym == symbol and start <= (event_ns, sequence)
                     and (end is None or event_ns < end))

    def allocate_placement(self, order: SimOrder) -> FillAllocation | None:
        """Spend one displayed ask pool across accounts, at placement not quote time.

        Displayed quotes and later prints are different evidence pools. Their
        native execution correspondence is unknown; this is a counterfactual,
        not a claim that the observed book would survive our intervention.
        """
        if order.model != self.model:
            raise ValueError("fill_model_ledger_mismatch")
        if self.model != FillModel.QUEUE_AWARE or order.placement_branch != "MARKETABLE_ASK":
            return None
        if self.unknown_evidence(order.identity.symbol, order.placed_at.event_ns, order.placed_at.sequence):
            return None
        if order.order_id in self._placements:
            raise ValueError("duplicate_placement_allocation")
        quote = order.placement_quote
        if quote is None:
            raise ValueError("placement_quote_required")
        from .entry import placement_book
        if placement_book(order.limit, quote) != ("MARKETABLE_ASK", 0):
            raise ValueError("invalid_marketable_placement_book")
        if quote.symbol != order.identity.symbol or (
            quote.key.event_ns, quote.key.sequence
        ) > (order.placed_at.event_ns, order.placed_at.sequence):
            raise ValueError("future_or_wrong_symbol_placement_quote")
        if isinstance(order.quantity, bool) or not isinstance(order.quantity, int) or order.quantity <= 0:
            raise ValueError("invalid_sim_order")
        self._placements.add(order.order_id)
        raw = (quote.symbol, quote.key.raw_id)
        price, original, spent = self._asks.get(raw, (quote.ask, quote.ask_size, 0))
        if (price, original) != (quote.ask, quote.ask_size):
            raise ValueError("raw_ask_book_changed")
        quantity = min(order.quantity, original - spent)
        self._asks[raw] = (price, original, spent + quantity)
        self._filled[order.order_id] = quantity
        if not quantity:
            return None
        at = replace(order.placed_at, raw_id=f"SIM_ASK:{order.order_id}:{quote.key.raw_id}", source=quote.key.source)
        return FillAllocation(order.order_id, at, price, quantity, 0,
                              "HYPOTHETICAL_SHARED_DISPLAYED_ASK_AT_PLACEMENT_NOT_NATIVE")

    def allocate(self, order: SimOrder, trade: TradePrint) -> FillAllocation | None:
        if order.model != self.model:
            raise ValueError("fill_model_ledger_mismatch")
        if trade.symbol != order.identity.symbol:
            return None
        if trade.size < 0 or isinstance(trade.size, bool) or not isinstance(trade.size, int):
            raise ValueError("nonnegative_integer_size_required")
        if not trade.price.is_finite() or trade.price <= 0:
            raise ValueError("positive_finite_price_required")
        if (trade.key.event_ns, trade.key.sequence) <= (
            order.placed_at.event_ns, order.placed_at.sequence
        ):
            return None
        if order.valid_until_ns is not None and trade.key.event_ns >= order.valid_until_ns:
            return None
        if trade.price > order.limit:
            return None
        if order.quantity <= 0 or not order.limit.is_finite() or order.limit <= 0:
            raise ValueError("invalid_sim_order")
        if self.model == FillModel.QUEUE_AWARE and order.queue_ahead is None:
            return None  # Engine must surface UNKNOWN_QUEUE_DEPTH, never assume zero.
        remaining = order.quantity - self._filled.get(order.order_id, 0)
        if remaining <= 0:
            return None
        status, _ = execution_status(trade)
        if status is None:
            self._unknown.setdefault(order.order_id, (trade.symbol, (trade.key.event_ns, trade.key.sequence), order.valid_until_ns,
                trade.execution or classify_execution(trade, None, None)))
        if status is not True or self.unknown_evidence(trade.symbol, trade.key.event_ns, trade.key.sequence):
            return None
        raw = (trade.symbol, trade.key.raw_id)
        pair = (order.order_id, raw)
        if pair in self._pairs:
            raise ValueError("duplicate_order_print_allocation")
        self._pairs.add(pair)
        original, spent = self._prints.get(raw, (trade.size, 0))
        if original != trade.size:
            raise ValueError("raw_print_size_changed")
        available = original - spent
        queue = self._queue.get(order.order_id, order.queue_ahead or 0)
        if queue < 0:
            raise ValueError("negative_queue_depth")
        queue_used = min(available, queue) if self.model == FillModel.QUEUE_AWARE else 0
        self._queue[order.order_id] = queue - queue_used
        available -= queue_used
        quantity = min(remaining, available)
        self._prints[raw] = (original, spent + queue_used + quantity)
        self._filled[order.order_id] = self._filled.get(order.order_id, 0) + quantity
        if quantity == 0 and queue_used == 0:
            return None
        return FillAllocation(
            order.order_id, trade.key, trade.price, quantity, queue_used,
            "HYPOTHETICAL_QUEUE_FIRST_PRINT_VOLUME_NOT_NATIVE"
            if self.model == FillModel.QUEUE_AWARE
            else "HYPOTHETICAL_FIRST_LATER_PRINT_VOLUME_NOT_NATIVE",
        )
