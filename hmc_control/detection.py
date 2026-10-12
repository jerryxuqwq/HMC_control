"""Read-only, best-effort module detection; these devices have no ID register.

Stable bias/configuration fingerprints are evidence, not a silicon identity or
presence guarantee. Never initialize a chip or reset board buffers to detect it.
"""
from .transport import HMCError

# (row, mask, accepted values). Exclude gain, power gates and tuneable channels.
# Bias alternatives cover installed GUI profiles and datasheet recommendations.
SIGNATURES = {
    "tx": ((1, 0xFF, (0x4A, 0xCA)), (3, 0xFC, (0xF4,)),
           (5, 0xFF, (0xBF, 0xFF)), (6, 0xFC, (0x6C, 0xEC)),
           (8, 0xFF, (0x8F,)), (10, 0x60, (0x40,)),
           (11, 7, (3,)), (19, 3, (2,)), (21, 0xF0, (0x10,)), (23, 0xE0, (0x60,))),
    "rx": ((3, 0x0F, (3,)), (4, 0xFE, (0x9E,)), (5, 0x0F, (0x0F,)),
           (6, 0xFF, (0xBF,)), (7, 0xFC, (0x6C,)), (8, 0xE0, (0x80,)),
           (19, 3, (2,)), (21, 0xF0, (0x10,)), (23, 0xE0, (0x60,))),
}


def detect_modules(transport):
    """Probe addresses 6/7 twice. Return likely tx/rx/both or unknown.

    At least 75% of fingerprints and three chip-specific fields must match.
    Fallback: stable all-zero reads opposite stable all-FF reads suggest an
    uninitialized module, using the user's observed motherboard behavior.
    Matching constants on both sides, instability and errors are inconclusive.
    The serial number alone identifies the USB bridge, not its fitted module.
    """
    probes = {}
    with transport.lock:
        for chip, address in (("tx", 6), ("rx", 7)):
            signature = SIGNATURES[chip]
            rows = [r for r, _, _ in signature]
            try:
                first = {r: transport.read_register(address, r) for r in rows}
                second = {r: transport.read_register(address, r) for r in reversed(rows)}
                matches = sum((first[r] & mask) in values for r, mask, values in signature)
                frontend = sum((first[r] & mask) in values for r, mask, values in signature if r < 16)
                stable = first == second
                varied = len(set(first.values())) >= 3
                likely = stable and varied and matches * 4 >= len(signature) * 3 and frontend >= 3
                reason = ("Stable configuration fingerprint" if likely else
                          "Unstable readback" if not stable else
                          "Constant/insufficiently varied readback" if not varied else
                          "Configuration not recognized")
                probes[chip] = dict(likely=likely, reason=reason, stable=stable, matches=matches,
                                    checked=len(signature), registers=first)
            except (HMCError, OSError) as exc:
                probes[chip] = dict(likely=False, reason="Read failed", error=str(exc))
    method = "read-only configuration fingerprint"
    if not any(p["likely"] for p in probes.values()) and all(p.get("stable") for p in probes.values()):
        for chip, other in (("tx", "rx"), ("rx", "tx")):
            if (set(probes[chip]["registers"].values()) == {0}
                    and set(probes[other]["registers"].values()) == {255}):
                probes[chip]["likely"] = True
                probes[chip]["reason"] = "Stable all-zero readback; opposite module reads all 0xFF (board-specific heuristic)"
                probes[other]["reason"] = "Stable all-0xFF readback, consistent with the observed disconnected module"
                method = "read-only zero/0xFF contrast heuristic"
                break
    found = [chip for chip, probe in probes.items() if probe["likely"]]
    # A failed probe makes any single-module conclusion ambiguous.
    kind = ("unknown" if any("error" in p for p in probes.values()) else
            "both" if len(found) == 2 else found[0] if found else "unknown")
    return dict(kind=kind, method=method, probes=probes)
