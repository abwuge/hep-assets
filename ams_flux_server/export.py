"""Export existing ROOT products using the scientific Python environment."""

import json
from pathlib import Path
import sys


def export(submission, destination):
    import ROOT

    ROOT.gROOT.SetBatch(True)
    destination.mkdir(exist_ok=True)
    source = ROOT.TFile.Open(str(submission / "01_Flux_Formula/results.root"))
    if not source or source.IsZombie():
        raise RuntimeError("Cannot open the final spectrum ROOT file")
    try:
        def required(name):
            obj = source.Get(name)
            if not obj:
                raise RuntimeError(f"Missing final spectrum object: {name}")
            return obj

        def vector(name, errors=False):
            hist = required(name)
            get = hist.GetBinError if errors else hist.GetBinContent
            return [get(i) for i in range(1, hist.GetNbinsX() + 1)]

        def matrix(name):
            hist = required(name)
            return [[hist.GetBinContent(j, i) for j in range(1, hist.GetNbinsX() + 1)]
                    for i in range(1, hist.GetNbinsY() + 1)]

        flux = required("flux")
        units = "m^-2 sr^-1 s^-1 GeV^-1"
        weighted_units = "m^-2 sr^-1 s^-1 GeV^2"
        result = {
            "energy_unit": "GeV",
            "energy_edges": [flux.GetBinLowEdge(i) for i in range(1, flux.GetNbinsX() + 2)],
            "representative_energy": vector("representative_energy"),
            "valid": vector("valid"),
            "flux": {"values": vector("flux"), "sigma": vector("flux", True),
                     "unit": units, "covariance": matrix("covariance"),
                     "covariance_unit": f"({units})^2"},
            "flux_e3": {"values": vector("flux_e3"), "sigma": vector("flux_e3", True),
                        "unit": weighted_units, "covariance": matrix("cov_e3"),
                        "covariance_unit": f"({weighted_units})^2"},
            "weighted_definition": required("weighted_definition").GetTitle(),
            "uncertainty_scope": required("uncertainty_scope").GetTitle(),
            "covariance_axes": "row and column follow the energy-bin order",
        }
        (destination / "spectrum.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
        for name in ("spectrum_canvas", "spectrum_e3_canvas"):
            required(name).SaveAs(str(destination / (name + ".png")))
    finally:
        source.Close()


if __name__ == "__main__":
    export(Path(sys.argv[1]), Path(sys.argv[2]))
