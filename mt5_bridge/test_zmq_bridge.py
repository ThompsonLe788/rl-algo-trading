"""Test the ZMQ PUB/SUB bridge locally (no MT5 needed).

Spawns a PUB server and a SUB client in-process to verify
message delivery, heartbeats, and JSON format.

Usage:
  python mt5_bridge/test_zmq_bridge.py
"""
import json
import time
import threading
import zmq
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import ZMQ_SIGNAL_ADDR, ZMQ_TOPIC
from mt5_bridge.signal_server import SignalServer, Signal


def subscriber_thread(addr: str, topic: bytes, results: list, duration: float):
    """Simulates the MQL5 EA SUB side."""
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.RCVTIMEO, 200)
    sub.setsockopt(zmq.LINGER, 0)
    sub.connect(addr)
    sub.subscribe(topic)

    deadline = time.time() + duration
    while time.time() < deadline:
        try:
            raw = sub.recv_string()
        except zmq.Again:
            continue

        # Strip topic
        space = raw.find(" ")
        if space < 0:
            continue
        payload = raw[space + 1:]
        try:
            data = json.loads(payload)
            results.append(data)
        except json.JSONDecodeError:
            print(f"  [SUB] Bad JSON: {payload[:80]}")

    sub.close()
    ctx.term()


def main():
    print("=" * 60)
    print("ZMQ PUB/SUB Bridge Test")
    print("=" * 60)

    # Use a test port to avoid collision
    test_addr = "tcp://127.0.0.1:5556"

    # Override config for test
    import config
    orig_addr = config.ZMQ_SIGNAL_ADDR
    config.ZMQ_SIGNAL_ADDR = test_addr

    received = []
    sub = threading.Thread(
        target=subscriber_thread,
        args=(test_addr, ZMQ_TOPIC, received, 8.0),
        daemon=True,
    )
    sub.start()

    # Give subscriber time to connect
    time.sleep(0.5)

    server = SignalServer(addr=test_addr, heartbeat_interval=2.0, file_fallback=False)
    time.sleep(1.0)  # PUB-SUB handshake

    # --- Test 1: Publish trading signals ---
    print("\n[1] Publishing 3 trading signals...")
    signals = [
        Signal(side=1,  price=2345.50, sl=2340.00, tp=2356.00, lot=0.10,
               regime=0, z_score=-2.1, win_prob=0.55, rr=2.0),
        Signal(side=-1, price=2345.50, sl=2351.00, tp=2334.00, lot=0.08,
               regime=1, z_score=2.3, win_prob=0.60, rr=2.1),
        Signal(side=0,  price=2345.50, sl=0, tp=0, lot=0,
               regime=0, z_score=0.1, win_prob=0, rr=0),
    ]
    for sig in signals:
        server.publish(sig)
        time.sleep(0.3)

    # Wait for heartbeats + delivery
    print("[2] Waiting for heartbeats (6s)...")
    time.sleep(6.0)

    server.close()
    sub.join(timeout=3)

    # Restore config
    config.ZMQ_SIGNAL_ADDR = orig_addr

    # --- Analyze results ---
    print(f"\n{'=' * 60}")
    print(f"Received {len(received)} messages total")

    heartbeats = [m for m in received if m.get("heartbeat")]
    trades = [m for m in received if not m.get("heartbeat")]

    print(f"  Trading signals: {len(trades)}")
    print(f"  Heartbeats:      {len(heartbeats)}")

    # Validate trading signals
    ok = True
    if len(trades) < 3:
        print(f"  FAIL: Expected 3 trading signals, got {len(trades)}")
        ok = False
    else:
        for i, t in enumerate(trades):
            expected = signals[i]
            if t["side"] != expected.side:
                print(f"  FAIL: Signal {i} side={t['side']}, expected {expected.side}")
                ok = False
            if abs(t["price"] - expected.price) > 0.01:
                print(f"  FAIL: Signal {i} price mismatch")
                ok = False
            if "timestamp" not in t:
                print(f"  FAIL: Signal {i} missing timestamp")
                ok = False

    # Validate heartbeats
    if len(heartbeats) < 2:
        print(f"  WARN: Expected >=2 heartbeats, got {len(heartbeats)}")
    else:
        for hb in heartbeats:
            if "timestamp" not in hb:
                print("  FAIL: Heartbeat missing timestamp")
                ok = False
            if "signal_count" not in hb:
                print("  FAIL: Heartbeat missing signal_count")
                ok = False

    # Print sample messages
    print(f"\n--- Sample trading signal ---")
    if trades:
        print(json.dumps(trades[0], indent=2))
    print(f"\n--- Sample heartbeat ---")
    if heartbeats:
        print(json.dumps(heartbeats[0], indent=2))

    if ok:
        print(f"\n{'=' * 60}")
        print("ALL CHECKS PASSED")
        print(f"{'=' * 60}")
    else:
        print(f"\n{'=' * 60}")
        print("SOME CHECKS FAILED")
        print(f"{'=' * 60}")
        sys.exit(1)


if __name__ == "__main__":
    main()
