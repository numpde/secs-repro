"""Recognize and decode the explicit scientific input formats admitted by SECS."""

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
import struct
from tempfile import TemporaryDirectory

import numpy as np

from secs_inference.provider.input_operations import SourceRef, source_document
from secs_inference.provider.source_access import (
    InputReadError, ReadSource, ScopeLimitError, ScopeReader, SourceAccess,
)
from secs_inference.spectra.bruker import read_bruker_pdata
from secs_inference.spectra.errors import SpectrumReadError
from secs_inference.spectra.jcamp import read_jcamp_spectrum
from secs_inference.spectra.secs import prepare_secs_spectrum

_LABEL = re.compile(r"^##([^=]+)=(.*)$")

@dataclass(frozen=True, slots=True)
class RepresentationComponent:
    role: str
    source: SourceRef
    digest: str

@dataclass(frozen=True, slots=True)
class DiscoveredRepresentation:
    key: str
    locator: str
    kind: str
    components: tuple[RepresentationComponent, ...]
    metadata: dict
    related_keys: tuple[str, ...] = ()

def read_declared_companions(reader: ScopeReader, selected: ReadSource) -> list[ReadSource]:
    """Read only same-dataset names established by the selected format."""
    member = selected.source.member
    if member is None:
        return []
    path = PurePosixPath(member)
    names: list[str] = []
    if path.name == "1r":
        names.append(str(path.with_name("procs")))
    elif path.name == "procs":
        names.append(str(path.with_name("1r")))
    elif path.name == "fid":
        names.extend((str(path.with_name("acqus")), str(path.with_name("procpar"))))
    elif path.name in {"acqus", "procpar"}:
        names.append(str(path.with_name("fid")))

    state = _nmrium_state(selected.contents)
    if state is not None:
        data = state.get("data")
        spectra = data.get("spectra") if type(data) is dict else None
        if type(spectra) is list:
            for spectrum in spectra:
                selector = spectrum.get("selector") if type(spectrum) is dict and type(spectrum.get("selector")) is dict else {}
                files = selector.get("files")
                root = selector.get("root")
                if (path.name == "state.json" and type(root) is str and root
                        and type(files) is list and len(files) == 1
                        and type(files[0]) is str and files[0]):
                    names.append(str(path.parent / "data" / root / files[0]))

    try:
        text = selected.contents.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    location = re.search(r"(?im)^Jcamp_location=file:([^\r\n]+)", text)
    if ">  <NMREDATA_VERSION>" in text and location is not None:
        names.append(str(path.parent / location.group(1).strip()))

    companions = []
    for name in dict.fromkeys(names):
        source = SourceRef(selected.source.upload_ref, name)
        try:
            companions.append(reader.read(source))
        except ScopeLimitError:
            raise
        except InputReadError:
            # The format-specific discovery path reports the missing role
            # against the selected source with a useful scientific name.
            pass
    return companions

def discover_representations(access: SourceAccess, reads: list[ReadSource]):
    """Return recognized choices and localized issues from one admitted scope."""
    discovered = []
    issues = []
    for read in reads:
        items, item_issues = _discover_standalone(access, read)
        discovered.extend(items)
        issues.extend(item_issues)
    items, item_issues = _discover_vendor_fids(reads)
    discovered.extend(items)
    issues.extend(item_issues)
    items, item_issues = _discover_bruker(access, reads)
    discovered.extend(items)
    issues.extend(item_issues)
    items, item_issues = _discover_nmrium(access, reads)
    discovered.extend(items)
    issues.extend(item_issues)
    discovered, annotation_issues = _associate_nmredata(discovered, reads)
    return discovered, issues + annotation_issues

def _discover_standalone(access: SourceAccess, read: ReadSource):
    blocks = _jcamp_blocks(read.contents)
    if not blocks:
        unsupported_nmrium = _unsupported_nmrium_version(read.contents)
        if unsupported_nmrium is not None:
            return [], [{"source": source_document(read.source), "reason": unsupported_nmrium}]
        jeol, issue = _discover_jeol(read)
        if jeol is not None:
            return [jeol], []
        if issue is not None:
            return [], [issue]
        return _discover_structures(read), []
    discovered = []
    issues = []
    block_keys = {}
    pending_relations = []
    for index, block in enumerate(blocks):
        labels = _jcamp_labels(block)
        data_type = _label(labels, "DATATYPE").replace(" ", "").upper()
        block_id = _label(labels, "BLOCKID") or str(index)
        key = f"{read.source.upload_ref}\0{read.source.member}\0jcamp\0{index}"
        block_keys[block_id] = key
        component = RepresentationComponent("data", read.source, read.digest)
        if "PEAKTABLE" in data_type:
            points = _integer_label(labels, "NPOINTS")
            metadata = {"nucleus": _nucleus(labels), "peaks": points,
                        "columns": ["shift", "height", "width"]}
            cross = _cross_reference(labels)
            pending_relations.append((key, cross))
            discovered.append(DiscoveredRepresentation(key, f"jcamp:{index}", "peak_table", (component,), metadata))
            continue
        if "NMRFID" in data_type:
            metadata = _jcamp_metadata(labels)
            try:
                with TemporaryDirectory(dir=access.directory, prefix="inspect-fid-") as directory:
                    path = Path(directory) / "spectrum.jdx"
                    path.write_bytes(block)
                    from secs_inference.spectra.fid import process_jcamp_fid
                    process_jcamp_fid(path)
            except SpectrumReadError as error:
                issues.append({
                    "source": source_document(read.source),
                    "reason": f"Cannot inspect this JCAMP-DX FID: {_safe_label(str(error))}",
                })
                continue
            discovered.append(DiscoveredRepresentation(key, f"jcamp:{index}", "fid", (component,), metadata))
            continue
        if "NMRSPECTRUM" not in data_type:
            continue
        metadata = _jcamp_metadata(labels)
        if metadata["dimension"] == 1:
            try:
                with TemporaryDirectory(dir=access.directory, prefix="inspect-jcamp-") as directory:
                    path = Path(directory) / "spectrum.jdx"
                    path.write_bytes(block)
                    read_jcamp_spectrum(path, required_nucleus=None)
            except SpectrumReadError:
                issues.append({
                    "source": source_document(read.source),
                    "reason": "Cannot inspect this source: malformed or incomplete spectrum data",
                })
                continue
        discovered.append(DiscoveredRepresentation(key, f"jcamp:{index}", "spectrum", (component,), metadata))
    if pending_relations:
        related = {key: [] for key, _ in pending_relations}
        for key, block_id in pending_relations:
            target = block_keys.get(block_id)
            if target is not None:
                related[key].append(target)
        discovered = [
            DiscoveredRepresentation(item.key, item.locator, item.kind, item.components, item.metadata,
                        tuple(related.get(item.key, ())))
            for item in discovered
        ]
    return discovered, issues

def _discover_nmrium(access: SourceAccess, reads: list[ReadSource]):
    discovered = []
    issues = []
    by_member = {read.source.member: read for read in reads if read.source.member is not None}
    for read in reads:
        state = _nmrium_state(read.contents)
        if state is None:
            continue
        data_section = state.get("data")
        spectra = data_section.get("spectra") if type(data_section) is dict else None
        if type(spectra) is not list:
            issues.append({"source": source_document(read.source),
                           "reason": "Cannot inspect this NMRium state: its spectrum inventory is malformed or missing"})
            continue
        declared_sources = data_section.get("sources", [])
        if type(declared_sources) is not list:
            issues.append({"source": source_document(read.source),
                           "reason": "Cannot inspect this NMRium state: its source inventory is malformed"})
            continue
        for index, spectrum in enumerate(spectra):
            if type(spectrum) is not dict:
                issues.append({"source": source_document(read.source),
                               "reason": "Cannot inspect this NMRium state: a spectrum entry is malformed"})
                continue
            info = spectrum.get("info") if type(spectrum.get("info")) is dict else {}
            components = [RepresentationComponent("state", read.source, read.digest)]
            locator = f"nmrium:{index}"
            spectrum_data = spectrum.get("data")
            if _nmrium_dense_data(spectrum_data):
                metadata = {
                    "dimension": info.get("dimension"),
                    "nucleus": _normalized_nucleus(info.get("nucleus")),
                    "points": len(_numeric_sequence(spectrum_data["x"])),
                }
                frequency = info.get("originFrequency", info.get("baseFrequency"))
                if type(frequency) in {int, float}:
                    try:
                        frequency = float(frequency)
                    except (OverflowError, TypeError, ValueError):
                        frequency = None
                    if frequency is not None and np.isfinite(frequency) and frequency > 0:
                        metadata["frequency_mhz"] = frequency
            elif type(spectrum_data) is dict and ({"x", "re"} & set(spectrum_data)):
                issues.append({"source": source_document(read.source),
                               "reason": "Cannot inspect this NMRium state: dense spectrum arrays are malformed or have different lengths"})
                continue
            else:
                selector = spectrum.get("selector") if type(spectrum.get("selector")) is dict else {}
                files = selector.get("files")
                root = selector.get("root")
                relative = files[0] if type(files) is list and len(files) == 1 and type(files[0]) is str else None
                state_path = PurePosixPath(read.source.member) if read.source.member is not None else None
                member = (str(state_path.parent / "data" / root / relative)
                          if state_path is not None and state_path.name == "state.json"
                          and type(root) is str and root and relative else None)
                resource = by_member.get(member) if member else None
                if resource is None:
                    name = _safe_label(relative) if relative else "the declared spectrum resource"
                    issues.append({"source": source_document(read.source),
                                   "reason": f"Cannot inspect this NMRium state: unavailable resource {name}"})
                    continue
                labels = _jcamp_labels(resource.contents)
                metadata = _jcamp_metadata(labels)
                try:
                    with TemporaryDirectory(dir=Path(access.directory), prefix="inspect-nmrium-") as directory:
                        path = Path(directory) / "spectrum.jdx"
                        path.write_bytes(resource.contents)
                        read_jcamp_spectrum(path, required_nucleus=None)
                except SpectrumReadError:
                    issues.append({"source": source_document(resource.source),
                                   "reason": "Cannot inspect this NMRium resource: malformed or incomplete spectrum data"})
                    continue
                components.append(RepresentationComponent("resource", resource.source, resource.digest))
                locator = f"nmrium-resource:{index}"
            key = f"{read.source.upload_ref}\0{read.source.member}\0nmrium\0{index}"
            discovered.append(DiscoveredRepresentation(key, locator, "spectrum", tuple(components), metadata))
    return discovered, issues

def _discover_bruker(access: SourceAccess, reads: list[ReadSource]):
    by_member = {read.source.member: read for read in reads if read.source.member is not None}
    discovered = []
    issues = []
    for member, data in by_member.items():
        if PurePosixPath(member).name != "1r":
            continue
        directory = str(PurePosixPath(member).parent)
        parameter_name = str(PurePosixPath(directory) / "procs")
        parameters = by_member.get(parameter_name)
        if parameters is None:
            issues.append({
                "source": source_document(data.source),
                "reason": "Cannot inspect this processed Bruker data: the matching procs companion is unavailable",
            })
            continue
        metadata = _bruker_metadata(parameters.contents)
        if metadata is None:
            issues.append({
                "source": source_document(parameters.source),
                "reason": "Cannot inspect this processed Bruker data: procs is malformed or incomplete",
            })
            continue
        try:
            with TemporaryDirectory(dir=access.directory, prefix="inspect-bruker-") as temporary:
                root = Path(temporary)
                (root / "1r").write_bytes(data.contents)
                (root / "procs").write_bytes(parameters.contents)
                read_bruker_pdata(root, required_nucleus=None, required_dimension=None)
        except SpectrumReadError:
            issues.append({
                "source": source_document(data.source),
                "reason": "Cannot inspect this processed Bruker data: 1r is malformed or incomplete",
            })
            continue
        components = (
            RepresentationComponent("data", data.source, data.digest),
            RepresentationComponent("parameters", parameters.source, parameters.digest),
        )
        key = f"{data.source.upload_ref}\0bruker\0{directory}"
        discovered.append(DiscoveredRepresentation(key, "bruker:processed", "spectrum", components, metadata))
    return discovered, issues

def _discover_vendor_fids(reads: list[ReadSource]):
    by_member = {read.source.member: read for read in reads if read.source.member is not None}
    discovered = []
    issues = []
    for member, data in by_member.items():
        path = PurePosixPath(member)
        if path.name != "fid":
            continue
        parent = str(path.parent)
        bruker = by_member.get(str(path.parent / "acqus"))
        varian = by_member.get(str(path.parent / "procpar"))
        if bruker is not None:
            metadata = _bruker_fid_metadata(bruker.contents)
            if metadata is None:
                issues.append({"source": source_document(bruker.source),
                               "reason": "Cannot inspect this Bruker dataset: acqus is malformed or incomplete"})
                continue
            if not metadata.pop("processing_supported"):
                issues.append({"source": source_document(bruker.source),
                               "reason": "Cannot inspect this Bruker FID: its digital-filter profile is unsupported"})
                continue
            if not _bruker_fid_size_valid(data.contents, bruker.contents):
                issues.append({"source": source_document(data.source),
                               "reason": "Cannot inspect this Bruker dataset: incomplete FID data"})
                continue
            components = (RepresentationComponent("data", data.source, data.digest),
                          RepresentationComponent("parameters", bruker.source, bruker.digest))
            discovered.append(DiscoveredRepresentation(
                f"{data.source.upload_ref}\0bruker-fid\0{parent}",
                "bruker:fid", "fid", components, metadata,
            ))
        elif varian is not None:
            metadata = _varian_fid_metadata(varian.contents)
            if metadata is None:
                issues.append({"source": source_document(varian.source),
                               "reason": "Cannot inspect this Varian dataset: procpar is malformed or incomplete"})
                continue
            if not metadata.pop("processing_supported"):
                issues.append({"source": source_document(varian.source),
                               "reason": "Cannot inspect this Varian FID: its chemical-shift reference profile is unsupported"})
                continue
            expected = 60 + metadata["points"] * 8
            if len(data.contents) < expected:
                issues.append({"source": source_document(data.source),
                               "reason": "Cannot inspect this Varian dataset: incomplete FID data"})
                continue
            components = (RepresentationComponent("data", data.source, data.digest),
                          RepresentationComponent("parameters", varian.source, varian.digest))
            discovered.append(DiscoveredRepresentation(
                f"{data.source.upload_ref}\0varian-fid\0{parent}",
                "varian:fid", "fid", components, metadata,
            ))
    return discovered, issues

def prepare_representation(access: SourceAccess, claim: dict, current: list[ReadSource]):
    """Decode the verified representation selected by the interpreter."""
    locator = claim["locator"]
    kind = claim["kind"]
    from_fid = False
    magnitude = False
    if locator.startswith("jcamp:"):
        source = current[0]
        block_index = int(locator.split(":", 1)[1])
        blocks = _jcamp_blocks(source.contents)
        try:
            block = blocks[block_index]
        except IndexError as error:
            raise InputReadError("Cannot execute this representation: its JCAMP block is no longer available") from error
        with TemporaryDirectory(dir=access.directory, prefix="jcamp-") as directory:
            path = Path(directory) / "spectrum.jdx"
            path.write_bytes(block)
            if kind == "fid":
                from secs_inference.spectra.fid import process_jcamp_fid
                spectrum, magnitude = process_jcamp_fid(path)
                from_fid = True
            else:
                spectrum = read_jcamp_spectrum(path)
            values = prepare_secs_spectrum(spectrum)
    elif locator == "bruker:processed":
        by_role = {component["role"]: read for component, read in zip(claim["components"], current)}
        with TemporaryDirectory(dir=access.directory, prefix="bruker-") as directory:
            root = Path(directory)
            (root / "1r").write_bytes(by_role["data"].contents)
            (root / "procs").write_bytes(by_role["parameters"].contents)
            values = prepare_secs_spectrum(read_bruker_pdata(root))
    elif locator == "bruker:fid":
        by_role = {component["role"]: read for component, read in zip(claim["components"], current)}
        spectrum, magnitude = _process_bruker_fid(by_role["data"].contents, by_role["parameters"].contents)
        values = prepare_secs_spectrum(spectrum)
        from_fid = True
    elif locator == "varian:fid":
        by_role = {component["role"]: read for component, read in zip(claim["components"], current)}
        spectrum, magnitude = _process_varian_fid(by_role["data"].contents, by_role["parameters"].contents)
        values = prepare_secs_spectrum(spectrum)
        from_fid = True
    elif locator.startswith("nmrium:"):
        index = int(locator.split(":", 1)[1])
        values = prepare_secs_spectrum(_nmrium_spectrum(current[0].contents, index))
    elif locator.startswith("nmrium-resource:"):
        index = int(locator.split(":", 1)[1])
        by_role = {component["role"]: read for component, read in zip(claim["components"], current)}
        with TemporaryDirectory(dir=access.directory, prefix="nmrium-") as directory:
            path = Path(directory) / "spectrum.jdx"
            path.write_bytes(by_role["resource"].contents)
            spectrum = read_jcamp_spectrum(path)
        shift = _nmrium_shift(by_role["state"].contents, index)
        if shift:
            from secs_inference.spectra.source import SourceSpectrum
            spectrum = SourceSpectrum(ppm=spectrum.ppm + shift, intensities=spectrum.intensities)
        values = prepare_secs_spectrum(spectrum)
    elif locator == "jeol:processed":
        values = prepare_secs_spectrum(_read_jeol(current[0].contents))
    else:
        raise InputReadError("Cannot execute this representation: its decoder is unavailable")
    return values, from_fid, magnitude

def _jcamp_blocks(contents: bytes) -> list[bytes]:
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError:
        return []
    if "##JCAMP" not in text.upper():
        return []
    lines = text.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.upper().startswith("##TITLE=")]
    if not starts:
        return []
    blocks = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        block = "".join(lines[start:end])
        labels = _jcamp_labels(block.encode("utf-8"))
        if _label(labels, "DATATYPE").replace(" ", "").upper() == "LINK":
            continue
        blocks.append(block.encode("utf-8"))
    return blocks

def _canonical_label(label: str) -> str:
    return re.sub(r"[\s\-_/._]", "", label).upper()

def _jcamp_labels(block: bytes) -> dict[str, str]:
    try:
        text = block.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    labels = {}
    for line in text.splitlines():
        match = _LABEL.match(line.strip())
        if match is not None:
            labels[_canonical_label(match.group(1))] = match.group(2).strip()
    return labels

def _label(labels: dict[str, str], name: str) -> str:
    return labels.get(_canonical_label(name), "")

def _integer_label(labels: dict[str, str], name: str) -> int:
    try:
        return int(_label(labels, name).split(",", 1)[0].strip())
    except ValueError:
        return 0

def _nucleus(labels: dict[str, str]):
    value = _label(labels, "OBSERVENUCLEUS") or _label(labels, "NUCLEUS")
    if not value:
        return None
    nuclei = [item.replace("^", "").strip() for item in value.split(",") if item.strip()]
    return nuclei[0] if len(nuclei) == 1 else nuclei

def _jcamp_metadata(labels: dict[str, str]) -> dict:
    dimension = _integer_label(labels, "NUMDIM") or 1
    points = _integer_label(labels, "NPOINTS")
    if not points:
        points = _integer_label(labels, "VARDIM")
    metadata = {"dimension": dimension, "nucleus": _nucleus(labels), "points": points}
    frequency = _label(labels, "OBSERVEFREQUENCY")
    if frequency:
        try:
            value = float(frequency)
        except (OverflowError, ValueError):
            pass
        else:
            if np.isfinite(value) and value > 0:
                metadata["frequency_mhz"] = value
    return metadata

def _cross_reference(labels: dict[str, str]) -> str:
    match = re.search(r"BLOCK_ID\s*=\s*([^,;\s]+)", _label(labels, "CROSSREFERENCE"), re.IGNORECASE)
    return match.group(1) if match is not None else ""

def _bruker_metadata(contents: bytes) -> dict | None:
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError:
        return None
    labels = _jcamp_labels(contents)
    nucleus = _label(labels, "$AXNUC").strip("<> ")
    try:
        dimension = int(_label(labels, "$PPARMOD")) + 1
        points = int(_label(labels, "$SI"))
        frequency = float(_label(labels, "$SF"))
        sweep_hz = float(_label(labels, "$SW_p"))
        offset = float(_label(labels, "$OFFSET"))
    except ValueError:
        return None
    byte_order_valid, byte_order = _optional_integer_label(labels, "$BYTORDP")
    data_type_valid, data_type = _optional_integer_label(labels, "$DTYPP")
    scale_valid, _ = _optional_float_label(labels, "$NC_proc")
    if (not text or dimension < 1 or points < 2
            or not all(np.isfinite(value) for value in (frequency, sweep_hz, offset))
            or frequency <= 0 or sweep_hz <= 0
            or not byte_order_valid or byte_order not in {None, 0, 1}
            or not data_type_valid or data_type not in {None, 0, 2}
            or not scale_valid):
        return None
    return {"dimension": dimension, "nucleus": nucleus or None,
            "points": points, "frequency_mhz": frequency}

def _optional_integer_label(labels: dict[str, str], name: str) -> tuple[bool, int | None]:
    value = _label(labels, name)
    if not value:
        return True, None
    try:
        return True, int(value)
    except ValueError:
        return False, None

def _optional_float_label(labels: dict[str, str], name: str) -> tuple[bool, float | None]:
    value = _label(labels, name)
    if not value:
        return True, None
    try:
        number = float(value)
    except ValueError:
        return False, None
    return (True, number) if np.isfinite(number) else (False, None)

def _bruker_fid_metadata(contents: bytes) -> dict | None:
    labels = _jcamp_labels(contents)
    try:
        points = int(_label(labels, "$TD")) // 2
        dimension = int(_label(labels, "$PARMODE")) + 1
        frequency = float(_label(labels, "$SFO1"))
        sweep = float(_label(labels, "$SW"))
        center = float(_label(labels, "$O1")) / frequency
        group_delay = float(_label(labels, "$GRPDLY"))
    except (ValueError, ZeroDivisionError):
        return None
    nucleus = _label(labels, "$NUC1").strip("<> ").replace("H1", "1H")
    if (points < 2 or not all(np.isfinite(value) for value in
                              (frequency, sweep, center, group_delay))
            or frequency <= 0 or sweep <= 0):
        return None
    return {"dimension": dimension, "nucleus": nucleus or None,
            "points": points, "frequency_mhz": frequency,
            "ppm_from": center - sweep / 2, "ppm_to": center + sweep / 2,
            "processing_supported": group_delay == 0}

def _bruker_fid_size_valid(data: bytes, parameters: bytes) -> bool:
    labels = _jcamp_labels(parameters)
    try:
        values = int(_label(labels, "$TD"))
        bytes_per_value = {0: 4, 2: 8}[int(_label(labels, "$DTYPA"))]
    except (KeyError, ValueError):
        return False
    return values >= 4 and len(data) >= values * bytes_per_value

def _process_bruker_fid(data: bytes, parameters: bytes):
    from secs_inference.spectra.errors import SpectrumReadError
    from secs_inference.spectra.fid import process_complex_fid

    labels = _jcamp_labels(parameters)
    try:
        values = int(_label(labels, "$TD"))
        dtype_code = int(_label(labels, "$DTYPA"))
        byte_order = int(_label(labels, "$BYTORDA"))
        sweep_ppm = float(_label(labels, "$SW"))
        frequency = float(_label(labels, "$SFO1"))
        center_ppm = float(_label(labels, "$O1")) / frequency
        dtype = {(0, 0): "<i4", (0, 1): ">i4", (2, 0): "<f8", (2, 1): ">f8"}[
            (dtype_code, byte_order)
        ]
    except (KeyError, ValueError, ZeroDivisionError) as error:
        raise SpectrumReadError("Cannot process this Bruker FID because its acquisition parameters are unsupported or incomplete") from error
    if values < 4 or values % 2 or len(data) < values * np.dtype(dtype).itemsize:
        raise SpectrumReadError("Cannot process this Bruker FID because its complex trace is incomplete")
    decoded = np.frombuffer(data, dtype=dtype, count=values).astype(np.float64)
    return process_complex_fid(decoded[0::2], decoded[1::2], sweep_ppm, frequency, center_ppm)

def _varian_fid_metadata(contents: bytes) -> dict | None:
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError:
        return None
    values = {}
    lines = text.splitlines()
    index = 0
    while index + 1 < len(lines):
        header = lines[index].split()
        if len(header) >= 3 and lines[index + 1].startswith("1 "):
            values[header[0]] = lines[index + 1][2:].strip().strip('"')
            index += 3
        else:
            index += 1
    try:
        points = int(values["np"]) // 2
        frequency = float(values["sfrq"])
        sweep_hz = float(values["sw"])
        reference_hz = float(values["rfl"]) - float(values["rfp"])
        reference_frequency = float(values["reffrq"])
    except (KeyError, ValueError, ZeroDivisionError):
        return None
    if (points < 2 or not all(np.isfinite(value) for value in
                              (frequency, sweep_hz, reference_hz, reference_frequency))
            or frequency <= 0 or sweep_hz <= 0 or reference_frequency <= 0):
        return None
    nucleus = {"H1": "1H", "C13": "13C"}.get(values.get("tn"), values.get("tn"))
    center = (reference_hz - sweep_hz / 2) / frequency
    supported = (np.isclose(reference_frequency, frequency)
                 and np.isclose(reference_hz, sweep_hz / 2))
    return {"dimension": 1, "nucleus": nucleus, "points": points,
            "frequency_mhz": frequency, "ppm_from": center - sweep_hz / frequency / 2,
            "ppm_to": center + sweep_hz / frequency / 2,
            "processing_supported": bool(supported)}

def _varian_parameters(contents: bytes) -> dict[str, str]:
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    values = {}
    lines = text.splitlines()
    index = 0
    while index + 1 < len(lines):
        header = lines[index].split()
        if len(header) >= 3 and lines[index + 1].startswith("1 "):
            values[header[0]] = lines[index + 1][2:].strip().strip('"')
            index += 3
        else:
            index += 1
    return values

def _process_varian_fid(data: bytes, parameters: bytes):
    from secs_inference.spectra.errors import SpectrumReadError
    from secs_inference.spectra.fid import process_complex_fid

    values = _varian_parameters(parameters)
    try:
        points = int(values["np"])
        sweep_ppm = float(values["sw"]) / float(values["sfrq"])
        frequency = float(values["sfrq"])
        nblocks, ntraces, header_points, bytes_per_value, _, bytes_per_block = struct.unpack_from(">6i", data, 0)
        _, status, block_headers = struct.unpack_from(">2hi", data, 24)
    except (KeyError, ValueError, struct.error, ZeroDivisionError) as error:
        raise SpectrumReadError("Cannot process this Varian FID because its acquisition parameters are unsupported or incomplete") from error
    if (nblocks != 1 or ntraces != 1 or header_points != points or points < 4 or points % 2
            or block_headers < 1 or bytes_per_block > len(data)):
        raise SpectrumReadError("Cannot process this Varian FID because its complex trace is incomplete or unsupported")
    dtype = ">f4" if bytes_per_value == 4 and status & 0x8 else ">i4" if bytes_per_value == 4 else None
    offset = 32 + block_headers * 28
    if dtype is None or offset + points * bytes_per_value > len(data):
        raise SpectrumReadError("Cannot process this Varian FID because its numeric encoding is unsupported or incomplete")
    decoded = np.frombuffer(data, dtype=dtype, count=points, offset=offset).astype(np.float64)
    reference_hz = float(values["rfl"]) - float(values["rfp"])
    center_ppm = (reference_hz - float(values["sw"]) / 2) / frequency
    return process_complex_fid(decoded[0::2], decoded[1::2], sweep_ppm, frequency, center_ppm)

def _discover_jeol(read: ReadSource):
    if not read.contents.startswith(b"JEOL.NMR"):
        return None, None
    source = source_document(read.source)
    if len(read.contents) < 1296:
        return None, {"source": source,
                      "reason": "Cannot inspect this JEOL source: incomplete spectrum data"}
    try:
        dimension = read.contents[12]
        axis_unit = read.contents[33]
        points = struct.unpack_from(">I", read.contents, 176)[0]
        data_offset = struct.unpack_from(">I", read.contents, 1284)[0]
        frequency = struct.unpack_from(">d", read.contents, 1064)[0]
        axis_start = struct.unpack_from(">d", read.contents, 272)[0]
        axis_stop = struct.unpack_from(">d", read.contents, 336)[0]
    except struct.error:
        return None, {"source": source,
                      "reason": "Cannot inspect this JEOL source: incomplete spectrum data"}
    if dimension != 1:
        return None, {"source": source,
                      "reason": f"Cannot inspect this JEOL source: {dimension}D data are unsupported; SECS requires 1D data"}
    if axis_unit != 26:
        return None, {"source": source,
                      "reason": "Cannot inspect this JEOL source: its stored axis is not in ppm"}
    if (points < 2 or data_offset + points * 8 > len(read.contents)
            or not all(np.isfinite(value) for value in (frequency, axis_start, axis_stop))
            or frequency <= 0
            or axis_start == axis_stop):
        return None, {"source": source,
                      "reason": "Cannot inspect this JEOL source: incomplete spectrum data"}
    nucleus = read.contents[808:816].split(b"\0", 1)[0].decode("ascii", errors="ignore").strip()
    item = DiscoveredRepresentation(
        f"{read.source.upload_ref}\0{read.source.member}\0jeol",
        "jeol:processed", "spectrum", (RepresentationComponent("data", read.source, read.digest),),
        {"dimension": dimension, "nucleus": nucleus or None, "points": points,
         "frequency_mhz": frequency, "ppm_from": axis_start, "ppm_to": axis_stop},
    )
    return item, None

def _read_jeol(contents: bytes):
    from secs_inference.spectra.source import SourceSpectrum

    if not contents.startswith(b"JEOL.NMR") or len(contents) < 1296:
        raise InputReadError("Cannot execute this JEOL representation: its spectrum data are incomplete")
    if contents[12] != 1 or contents[33] != 26:
        raise InputReadError("Cannot execute this JEOL representation: it is not one-dimensional data with a stored ppm axis")
    points = struct.unpack_from(">I", contents, 176)[0]
    data_offset = struct.unpack_from(">I", contents, 1284)[0]
    axis_start = struct.unpack_from(">d", contents, 272)[0]
    axis_stop = struct.unpack_from(">d", contents, 336)[0]
    if (points < 2 or data_offset + points * 8 > len(contents)
            or not np.isfinite(axis_start) or not np.isfinite(axis_stop)
            or axis_start == axis_stop):
        raise InputReadError("Cannot execute this JEOL representation: its spectrum data are incomplete")
    intensities = np.frombuffer(contents, dtype=">f8", count=points, offset=data_offset).astype(np.float64)
    return SourceSpectrum(
        ppm=np.linspace(axis_start, axis_stop, points, dtype=np.float64),
        intensities=intensities,
    )

def _discover_structures(read: ReadSource) -> list[DiscoveredRepresentation]:
    """Decode unambiguous structure records; plain SMILES require full-line validity."""
    try:
        text = read.contents.decode("utf-8")
    except UnicodeDecodeError:
        return []
    from rdkit import Chem, rdBase
    from rdkit.Chem import rdMolDescriptors

    records: list[tuple[str, object]] = []
    with rdBase.BlockLogs():
        if "M  END" in text:
            blocks = text.split("$$$$") if "$$$$" in text else [text]
            for index, block in enumerate(blocks):
                if not block.strip():
                    continue
                molecule = Chem.MolFromMolBlock(block.strip("\r\n") + "\n", sanitize=True, removeHs=False)
                if molecule is None:
                    return []
                records.append((f"mol:{index}", molecule))
        else:
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if not lines or any(any(character.isspace() for character in line) for line in lines):
                return []
            for index, line in enumerate(lines):
                molecule = Chem.MolFromSmiles(line)
                if molecule is None:
                    return []
                records.append((f"smiles:{index}", molecule))
    component = RepresentationComponent("structure", read.source, read.digest)
    return [
        DiscoveredRepresentation(
            f"{read.source.upload_ref}\0{read.source.member}\0{locator}",
            locator,
            "structure",
            (component,),
            {"formula": rdMolDescriptors.CalcMolFormula(molecule)},
        )
        for locator, molecule in records
    ]

def _nmrium_state(contents: bytes) -> dict | None:
    try:
        document = json.loads(contents)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (type(document) is not dict or document.get("version") != 21
            or type(document.get("data")) is not dict):
        return None
    return document

def _unsupported_nmrium_version(contents: bytes) -> str | None:
    """Identify state-shaped JSON outside the one qualified NMRium schema."""
    try:
        document = json.loads(contents)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (type(document) is dict and type(document.get("version")) is int
            and type(document.get("data")) is dict and document["version"] != 21):
        return (
            "Cannot inspect this NMRium state: schema version "
            f"{document['version']} is unsupported; this provider accepts version 21"
        )
    return None

def _numeric_sequence(value: object) -> list[float]:
    if type(value) is list:
        sequence = value
    elif type(value) is dict:
        try:
            sequence = [value[str(index)] for index in range(len(value))]
        except KeyError:
            return []
    else:
        return []
    converted = []
    for item in sequence:
        if type(item) not in {int, float}:
            return []
        try:
            number = float(item)
        except (OverflowError, TypeError, ValueError):
            return []
        if not np.isfinite(number):
            return []
        converted.append(number)
    return converted

def _nmrium_dense_data(data: object) -> bool:
    if type(data) is not dict:
        return False
    x = _numeric_sequence(data.get("x"))
    real = _numeric_sequence(data.get("re"))
    return len(x) >= 2 and len(x) == len(real)

def _safe_label(value: str, limit: int = 120) -> str:
    """Quote bounded printable evidence while exact identities stay structured."""
    printable = "".join(character if character.isprintable() else "�" for character in value)
    if len(printable) > limit:
        printable = printable[:limit - 1] + "…"
    return json.dumps(printable, ensure_ascii=False)

def safe_text(value: str, limit: int = 120) -> str:
    """Bound one unquoted diagnostic fragment and remove line-forging controls."""
    printable = "".join(character if character.isprintable() else "�" for character in value)
    return printable if len(printable) <= limit else printable[:limit - 1] + "…"

def _normalized_nucleus(value: object):
    if type(value) is not str or not value.strip():
        return None
    return value.replace("^", "").strip()

def _nmrium_spectrum(contents: bytes, index: int):
    from secs_inference.spectra.source import SourceSpectrum

    state = _nmrium_state(contents)
    try:
        spectrum = state["data"]["spectra"][index]
        x = np.asarray(_numeric_sequence(spectrum["data"]["x"]), dtype=np.float64)
        intensities = np.asarray(_numeric_sequence(spectrum["data"]["re"]), dtype=np.float64)
    except (TypeError, KeyError, IndexError) as error:
        raise InputReadError("Cannot execute this NMRium representation: its stored spectrum is unavailable") from error
    if x.size < 2 or x.size != intensities.size:
        raise InputReadError("Cannot execute this NMRium representation: its stored spectrum is incomplete")
    shift = _nmrium_shift(contents, index)
    return SourceSpectrum(ppm=x + shift, intensities=intensities)

def _nmrium_shift(contents: bytes, index: int) -> float:
    state = _nmrium_state(contents)
    try:
        spectrum = state["data"]["spectra"][index]
    except (TypeError, KeyError, IndexError) as error:
        raise InputReadError("Cannot execute this NMRium representation: its stored processing is unavailable") from error
    processings = spectrum.get("processings", [])
    if type(processings) is not list:
        raise InputReadError("Cannot execute this NMRium representation: its stored processing is malformed")
    shift = 0.0
    for operation in processings:
        if type(operation) is not dict or operation.get("enabled") is False:
            continue
        if operation.get("operatorId") != "@zakodium/nmrium-core-plugins#shiftX1D":
            raise InputReadError("Cannot execute this NMRium representation: it uses an unsupported stored processing operation")
        setting = operation.get("settings")
        if type(setting) not in {int, float}:
            raise InputReadError("Cannot execute this NMRium representation: its stored shift is malformed")
        try:
            number = float(setting)
        except (OverflowError, TypeError, ValueError) as error:
            raise InputReadError("Cannot execute this NMRium representation: its stored shift is malformed") from error
        if not np.isfinite(number):
            raise InputReadError("Cannot execute this NMRium representation: its stored shift is malformed")
        shift += number
    return shift

def _associate_nmredata(items: list[DiscoveredRepresentation], reads: list[ReadSource]):
    """Attach the tested NMReDATA 1.0 resource and declared assignments."""
    by_source = {
        (read.source.upload_ref, read.source.member): read for read in reads
    }
    result = list(items)
    issues = []
    for read in reads:
        try:
            text = read.contents.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if ">  <NMREDATA_VERSION>" not in text:
            continue
        location_match = re.search(r"(?im)^Jcamp_location=file:([^\r\n]+)", text)
        if location_match is None:
            continue
        relative = location_match.group(1).strip()
        parent = PurePosixPath(read.source.member).parent if read.source.member is not None else None
        member = str(parent / relative) if parent is not None else None
        resource = by_source.get((read.source.upload_ref, member)) if member is not None else None
        structure_indexes = [index for index, item in enumerate(result)
                             if item.kind == "structure"
                             and any(component.source == read.source for component in item.components)]
        if resource is None:
            issues.append({"source": source_document(read.source),
                           "reason": f"Cannot inspect these NMReDATA annotations: unavailable resource {_safe_label(relative)}"})
            continue
        spectrum_indexes = [index for index, item in enumerate(result)
                            if item.kind == "spectrum"
                            and any(component.source == resource.source for component in item.components)]
        if len(structure_indexes) != 1 or len(spectrum_indexes) != 1:
            continue
        annotations = _nmredata_annotations(text)
        structure = result[structure_indexes[0]]
        spectrum = result[spectrum_indexes[0]]
        annotation_component = RepresentationComponent("annotations", read.source, read.digest)
        metadata = dict(spectrum.metadata)
        metadata["annotations"] = annotations
        components = spectrum.components
        if all(component.source != read.source for component in components):
            components += (annotation_component,)
        result[spectrum_indexes[0]] = DiscoveredRepresentation(
            spectrum.key, spectrum.locator, spectrum.kind, components, metadata,
            tuple(dict.fromkeys(spectrum.related_keys + (structure.key,))),
        )
    return result, issues

def _nmredata_annotations(text: str) -> list[dict]:
    assignments = {}
    assignment_match = re.search(
        r">\s*<NMREDATA_ASSIGNMENT>\s*\r?\n(.*?)(?:\r?\n){2}", text, re.DOTALL
    )
    if assignment_match is not None:
        for line in assignment_match.group(1).splitlines():
            fields = [field.strip() for field in line.split(",")]
            if len(fields) >= 3:
                assignments[fields[0]] = fields[2:]
    section = re.search(r">\s*<NMREDATA_1D_1H>\s*\r?\n(.*?)(?:\r?\n){2}", text, re.DOTALL)
    if section is None:
        return []
    annotations = []
    for line in section.group(1).splitlines():
        if line.lower().startswith(("larmor=", "jcamp_location=")):
            continue
        fields = [field.strip() for field in line.split(",")]
        try:
            shift = float(fields[0])
        except (IndexError, ValueError):
            continue
        values = {key.strip().upper(): value.strip() for field in fields[1:]
                  if "=" in field for key, value in [field.split("=", 1)]}
        label = values.get("L")
        try:
            count = int(values["N"])
        except (KeyError, ValueError):
            count = None
        annotation = {"shift": shift, "multiplicity": values.get("S"), "atom_count": count}
        if label is not None:
            annotation["assignment"] = {"label": label, "atoms": assignments.get(label, [])}
        annotations.append(annotation)
    return annotations
