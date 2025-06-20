#  SKY130 plugin for Hammer.
#
#  See LICENSE for licence details.

import functools
import importlib
import importlib.resources
import json
import os
import re
import shutil
from pathlib import Path
from typing import List

from hammer.tech import (
    Corner,
    Decimal,
    DRCDeck,
    HammerTechnology,
    Library,
    LVSDeck,
    Metal,
    PathPrefix,
    Provide,
    Site,
    Stackup,
    Supplies,
    TechConfig,
)
from hammer.tech.specialcells import CellType, SpecialCell
from hammer.utils import LEFUtils
from hammer.vlsi import (
    HammerDRCTool,
    HammerLVSTool,
    HammerPlaceAndRouteTool,
    HammerTool,
    HammerToolHookAction,
    HierarchicalMode,
    TCLTool,
)


class SKY130Tech(HammerTechnology):
    """
    Override the HammerTechnology used in `hammer_tech.py`
    This class is loaded by function `load_from_json`, and will pass the `try` in `importlib`.
    """

    def gen_config(self) -> None:
        """Generate the tech config, based on the library type selected"""
        slib = self.get_setting("technology.sky130.stdcell_library")
        SKY130A = self.get_setting("technology.sky130.sky130A")
        SKY130_CDS = self.get_setting("technology.sky130.sky130_cds")
        SKY130_SCL = self.get_setting("technology.sky130.sky130_scl")

        self.use_sram22 = os.path.exists(
            self.get_setting("technology.sky130.sram22_sky130_macros")
        )

        # Common tech LEF and IO cell spice netlists
        libs = []
        if slib == "sky130_fd_sc_hd":
            libs += [
                Library(
                    lef_file=os.path.join(
                        SKY130A,
                        "libs.ref/sky130_fd_sc_hd/techlef/sky130_fd_sc_hd__nom.tlef",
                    ),
                    verilog_sim=os.path.join(
                        SKY130A, "libs.ref/sky130_fd_sc_hd/verilog/primitives.v"
                    ),
                    provides=[Provide(lib_type="technology")],
                ),
            ]
        elif slib == "sky130_scl":
            libs += [
                Library(
                    lef_file=os.path.join(SKY130_SCL, "sky130_scl_9T_tech/lef/sky130_scl_9T.tlef"),
                    verilog_sim=os.path.join(SKY130_SCL, "sky130_scl_9T/verilog/sky130_scl_9T.v"),
                    provides=[Provide(lib_type="technology")],
                ),
            ]
            libs += [
                Library(
                    lef_file=os.path.join(SKY130_SCL, "sky130_scl_9T_tech/lef/sky130_scl_9T_phyCells.lef"),
                    provides=[Provide(lib_type="technology")],
                ),
            ]
        else:
            raise ValueError(f"Incorrect standard cell library selection: {slib}")
        if self.use_sram22:
            libs += [
                Library(
                    spice_file=os.path.join(
                        self.get_setting("technology.sky130.sram22_sky130_macros"),
                        "sram22.spice",
                    ),
                    provides=[Provide(lib_type="technology")],
                ),
            ]

        # Stdcell library-dependent lists
        stackups = []  # type: List[Stackup]
        phys_only = []  # type: List[Cell]
        dont_use = []  # type: List[Cell]
        spcl_cells = []  # type: List[SpecialCell]

        # base path -> list of corners
        lib_corner_files = {}

        # Select standard cell libraries
        if slib == "sky130_fd_sc_hd":
            phys_only = [
                "sky130_fd_sc_hd__tap_1",
                "sky130_fd_sc_hd__tap_2",
                "sky130_fd_sc_hd__tapvgnd_1",
                "sky130_fd_sc_hd__tapvpwrvgnd_1",
                "sky130_fd_sc_hd__fill_1",
                "sky130_fd_sc_hd__fill_2",
                "sky130_fd_sc_hd__fill_4",
                "sky130_fd_sc_hd__fill_8",
                "sky130_fd_sc_hd__diode_2",
            ]
            dont_use = [
                "*sdf*",
                "sky130_fd_sc_hd__probe_p_*",
                "sky130_fd_sc_hd__probec_p_*",
            ]
            spcl_cells = [
                # for now, skipping the tiecell step. I think the extracted verilog netlist is ignoring the tiecells which is causing lvs issues
                SpecialCell(
                    cell_type=CellType("tiehilocell"), name=["sky130_fd_sc_hd__conb_1"]
                ),
                SpecialCell(
                    cell_type=CellType("tiehicell"),
                    name=["sky130_fd_sc_hd__conb_1"],
                    output_ports=["HI"],
                ),
                SpecialCell(
                    cell_type=CellType("tielocell"),
                    name=["sky130_fd_sc_hd__conb_1"],
                    output_ports=["LO"],
                ),
                SpecialCell(
                    cell_type=CellType("endcap"), name=["sky130_fd_sc_hd__tap_1"]
                ),
                SpecialCell(
                    cell_type=CellType("tapcell"),
                    name=["sky130_fd_sc_hd__tapvpwrvgnd_1"],
                ),
                SpecialCell(
                    cell_type=CellType("stdfiller"),
                    name=[
                        "sky130_fd_sc_hd__fill_1",
                        "sky130_fd_sc_hd__fill_2",
                        "sky130_fd_sc_hd__fill_4",
                        "sky130_fd_sc_hd__fill_8",
                    ],
                ),
                SpecialCell(
                    cell_type=CellType("decap"),
                    name=[
                        "sky130_fd_sc_hd__decap_3",
                        "sky130_fd_sc_hd__decap_4",
                        "sky130_fd_sc_hd__decap_6",
                        "sky130_fd_sc_hd__decap_8",
                        "sky130_fd_sc_hd__decap_12",
                    ],
                ),
                SpecialCell(
                    cell_type=CellType("driver"),
                    name=["sky130_fd_sc_hd__buf_4"],
                    input_ports=["A"],
                    output_ports=["X"],
                ),
                # this breaks synthesis with a complaint about "Cannot perform synthesis because libraries do not have usable inverters." from Genus.
                # note that innovus still recognizes and uses this cell as a buffer
                # SpecialCell(
                # cell_type=CellType("ctsbuffer"), name=["sky130_fd_sc_hd__clkbuf_1"]
                # ),
            ]

            # Generate standard cell library
            library = slib

            # scl vs 130a have different site names
            sites = None

            STDCELL_LIBRARY_BASE_PATH = os.path.join(SKY130A, "libs.ref", library)
            lib_corner_files[STDCELL_LIBRARY_BASE_PATH] = os.listdir(
                os.path.join(STDCELL_LIBRARY_BASE_PATH, "lib")
            )
            lib_corner_files[STDCELL_LIBRARY_BASE_PATH].sort()

            # Generate stackup
            tlef_path = os.path.join(
                SKY130A, "libs.ref", library, "techlef", f"{library}__min.tlef"
            )
            metals = list(
                map(lambda m: Metal.model_validate(m), LEFUtils.get_metals(tlef_path))
            )
            stackups.append(
                Stackup(name=slib, grid_unit=Decimal("0.001"), metals=metals)
            )

            sites = [
                Site(name="unithd", x=Decimal("0.46"), y=Decimal("2.72")),
                Site(name="unithddbl", x=Decimal("0.46"), y=Decimal("5.44")),
            ]
            lvs_decks = [
                LVSDeck(
                    tool_name="calibre",
                    deck_name="calibre_lvs",
                    path="$SKY130_NDA/s8/V2.0.1/LVS/Calibre/lvsRules_s8",
                ),
                LVSDeck(
                    tool_name="pegasus",
                    deck_name="pegasus_lvs",
                    path="$SKY130_CDS/Sky130_LVS/sky130.lvs.pvl",
                ),
            ]
            drc_decks = [
                DRCDeck(
                    tool_name=self.get_setting("vlsi.core.drc_tool").replace(
                        "hammer.drc.", ""
                    ),
                    deck_name=f"{self.get_setting('vlsi.core.drc_tool').replace('hammer.drc.', '')}_drc",
                    path=path,
                )
                for path in self.get_setting("technology.sky130.drc_deck_sources")
            ]

        elif slib == "sky130_scl":
            # note: you need to manually include io cells in design.yml if using scl

            # The cadence PDK (as of version 0.0.3) doesn't seem to have tap nor decap cells, so par won't run (and if we forced it to, lvs would fail)
            spcl_cells = [
                SpecialCell(
                    cell_type="stdfiller", name=[f"FILL{i**2}" for i in range(7)]
                ),
                SpecialCell(
                    cell_type="driver",
                    name=["TBUFX1", "TBUFX4", "TBUFX8"],
                    input_ports=["A"],
                    output_ports=["Y"],
                ),
                # this breaks synthesis with a complaint about "Cannot perform synthesis because libraries do not have usable inverters." from Genus.
                # note that innovus still recognizes and uses this cell as a buffer
                # added for par with a hook `set_cts_base_cells`
                # SpecialCell(
                # cell_type="ctsbuffer", name=["CLKBUFX2", "CLKBUFX4", "CLKBUFX8"]
                # ),
                # SpecialCell(cell_type=CellType("ctsgate"), name=["ICGX1"]),
                SpecialCell(
                    cell_type=CellType("tiehicell"), name=["TIEHI"], input_ports=["Y"]
                ),
                SpecialCell(
                    cell_type=CellType("tielocell"), name=["TIELO"], input_ports=["Y"]
                ),
            ]

            # Generate standard cell library
            library = slib
            STDCELL_LIBRARY_BASE_PATH = os.path.join(SKY130_SCL, "sky130_scl_9T")
            lib_corner_files[STDCELL_LIBRARY_BASE_PATH] = os.listdir(
                os.path.join(STDCELL_LIBRARY_BASE_PATH, "lib")
            )
            lib_corner_files[STDCELL_LIBRARY_BASE_PATH].sort()

            # Generate stackup
            metals = []  # type: List[Metal]

            tlef_path = os.path.join(SKY130_SCL, "sky130_scl_9T_tech", "lef", f"{slib}_9T.tlef")
            metals = list(
                map(lambda m: Metal.model_validate(m), LEFUtils.get_metals(tlef_path))
            )
            stackups.append(
                Stackup(name=slib, grid_unit=Decimal("0.001"), metals=metals)
            )

            sites = [
                Site(name="CoreSite", x=Decimal("0.46"), y=Decimal("4.14")),
                Site(name="IOSite", x=Decimal("1.0"), y=Decimal("240.0")),
                Site(name="CornerSite", x=Decimal("240.0"), y=Decimal("240.0")),
            ]

            lvs_decks = [
                LVSDeck(
                    tool_name="pegasus",
                    deck_name="pegasus_lvs",
                    path=os.path.join(SKY130_CDS, "Sky130_LVS", "sky130.lvs.pvl"),
                )
            ]
            drc_decks = [
                DRCDeck(
                    tool_name=self.get_setting("vlsi.core.drc_tool").replace(
                        "hammer.drc.", ""
                    ),
                    deck_name=f"{self.get_setting('vlsi.core.drc_tool').replace('hammer.drc.', '')}_drc",
                    path=path,
                )
                for path in self.get_setting("technology.sky130.drc_deck_sources")
            ]

        else:
            raise ValueError(f"Incorrect standard cell library selection: {slib}")

        # add skywater io cells
        io_library_base_path = os.path.join(SKY130A, "libs.ref", "sky130_fd_io")
        if os.path.exists(io_library_base_path):
            lib_corner_files[io_library_base_path] = os.listdir(
                os.path.join(io_library_base_path, "lib")
            )

        for library_base_path, cornerfiles in lib_corner_files.items():
            for cornerfilename in cornerfiles:
                if "sky130" not in cornerfilename or "#" in cornerfilename:
                    # cadence doesn't use the lib name in their corner libs
                    # also skip random temp files sometimes included in sky130_scl
                    continue
                if "ccsnoise" in cornerfilename:
                    continue  # ignore duplicate corner.lib/corner_ccsnoise.lib files

                tmp = cornerfilename.replace(".lib", "").strip("_nldm")
                if tmp + "_ccsnoise.lib" in lib_corner_files:
                    cornerfilename = (
                        tmp + "_ccsnoise.lib"
                    )  # use ccsnoise version of lib file

                # different naming conventions
                if "sky130A" in library_base_path:
                    split_cell_corner = re.split("_(ff)|_(ss)|_(tt)", tmp)
                    cell_name = split_cell_corner[0]
                    library = tmp.split("__")[0]
                    process = split_cell_corner[1:-1]
                    temp_volt = split_cell_corner[-1].split("_")[1:]

                    # Filter out cross corners (e.g ff_ss or ss_ff)
                    if len(process) > 3:
                        if not functools.reduce(
                            lambda x, y: x and y,
                            map(lambda p, q: p == q, process[0:3], process[4:]),
                            True,
                        ):
                            continue
                    # Determine actual corner
                    speed = next(c for c in process if c is not None).replace("_", "")
                    temp = temp_volt[0]
                    temp = temp.replace("n", "-")
                    temp = temp.split("C")[0] + " C"

                    vdd = (".").join(temp_volt[1].split("v")) + " V"
                    if speed == "ff":
                        speed = "fast"
                    elif speed == "tt":
                        speed = "typical"
                    elif speed == "ss":
                        speed = "slow"
                    else:
                        self.logger.info(
                            "Skipping lib with unsupported corner: {}".format(speed)
                        )
                        continue
                else:
                    library = "sky130_scl_9T"
                    _, speed, vdd, temp, _ = tmp.split("_")

                    # force equivalent operating conditions for speed, since they're different between sky130a and scl
                    if speed == "ff":
                        temp = "-40 C"
                        vdd = "1.95 V"
                        speed = "fast"
                    if speed == "tt":
                        vdd = "1.80 V"
                        temp = "25 C"
                        speed = "typical"
                    if speed == "ss":
                        vdd = "1.60 V"
                        speed = "slow"
                        temp = "100 C"

                cdl_path = os.path.join(library_base_path, "cdl", library + ".cdl")
                spice_path = os.path.join(
                    library_base_path, "spice", library + ".spice"
                )

                # just prioritize spice, arbitrary choice
                # assert not (os.path.exists(cdl_path) and os.path.exists(spice_path)), "both spice and cdl netlists exist! this is ambiguous :("

                netlist_path = spice_path if os.path.exists(spice_path) else cdl_path

                lib_entry = Library(
                    nldm_liberty_file=os.path.join(
                        library_base_path, "lib", cornerfilename
                    ),
                    verilog_sim=os.path.join(
                        SKY130_SCL,
                        "sky130_scl_9T",
                        "verilog",
                        library + "_9T.v" if slib == "sky130_scl" else ".v",
                    ),
                    lef_file=os.path.join(library_base_path, "lef", library + ".lef"),
                    spice_file=netlist_path,
                    gds_file=os.path.join(library_base_path, "gds", library + ".gds"),
                    corner=Corner(nmos=speed, pmos=speed, temperature=temp),
                    supplies=Supplies(VDD=vdd, GND="0 V"),
                    provides=[Provide(lib_type="stdcell", vt="RVT")],
                )
                libs.append(lib_entry)

                if "sky130_fd_io" in library_base_path:
                    # these are seperate from the io gds
                    extra_gds_files = [
                        "sky130_ef_io__analog.gds",
                        "sky130_ef_io__disconnect_vccd_slice_5um.gds",
                        "sky130_ef_io__gpiov2_pad_wrapped.gds",
                        "sky130_ef_io__bare_pad.gds",
                        "sky130_ef_io__disconnect_vdda_slice_5um.gds",
                        "sky130_ef_io__connect_vcchib_vccd_and_vswitch_vddio_slice_20um.gds",
                        "sky130_ef_io.gds",
                    ]
                    for extra_gds_file in extra_gds_files:
                        lib_entry = Library(
                            nldm_liberty_file=os.path.join(
                                library_base_path, "lib", cornerfilename
                            ),
                            verilog_sim=os.path.join(
                                SKY130_SCL,
                                "sky130_scl_9T",
                                "verilog",
                                library + "_9T.v" if slib == "sky130_scl" else ".v",
                            ),
                            lef_file=os.path.join(
                                library_base_path, "lef", library + ".lef"
                            ),
                            spice_file=netlist_path,
                            gds_file=os.path.join(
                                library_base_path, "gds", extra_gds_file
                            ),
                            corner=Corner(nmos=speed, pmos=speed, temperature=temp),
                            supplies=Supplies(VDD=vdd, GND="0 V"),
                            provides=[Provide(lib_type="stdcell", vt="RVT")],
                        )
                        libs.append(lib_entry)

        self.config = TechConfig(
            name="Skywater 130nm Library",
            grid_unit="0.001",
            shrink_factor=None,
            installs=[
                PathPrefix(id="$SKY130_NDA", path="technology.sky130.sky130_nda"),
                PathPrefix(id="$SKY130A", path="technology.sky130.sky130A"),
                PathPrefix(id="$SKY130_CDS", path="technology.sky130.sky130_cds"),
                PathPrefix(id="$SKY130_SCL", path="technology.sky130.sky130_scl"),
            ],
            libraries=libs,
            gds_map_file="sky130_lefpin.map",
            physical_only_cells_list=phys_only,
            dont_use_list=dont_use,
            additional_drc_text="",
            lvs_decks=lvs_decks,
            drc_decks=drc_decks,
            additional_lvs_text="",
            tarballs=None,
            sites=sites,
            stackups=stackups,
            special_cells=spcl_cells,
            extra_prefixes=None,
        )

    def post_install_script(self) -> None:
        self.library_name = "sky130_fd_sc_hd"
        # check whether variables were overriden to point to a valid path
        if self.get_setting("technology.sky130.stdcell_library") == "sky130_fd_sc_hd":
            self.setup_cdl()
            self.setup_verilog()
        self.setup_techlef()
        self.setup_io_lefs()
        self.setup_calibre_lvs_deck()
        self.setup_hvl_ls_lef()
        print("Loaded Sky130 Tech")

    def setup_calibre_lvs_deck(self) -> bool:
        # Remove conflicting specification statements found in PDK LVS decks
        pattern = ".*({}).*\n".format("|".join(LVS_DECK_SCRUB_LINES))
        matcher = re.compile(pattern)

        source_paths = self.get_setting("technology.sky130.lvs_deck_sources")
        lvs_decks = self.config.lvs_decks
        if not lvs_decks:
            return True
        for i, deck in enumerate(lvs_decks):
            if deck.tool_name != "calibre":
                continue
            try:
                source_path = Path(source_paths[i])
            except IndexError:
                self.logger.error(
                    "No corresponding source for LVS deck {}".format(deck)
                )
                continue
            if not source_path.exists():
                raise FileNotFoundError(f"LVS deck not found: {source_path}")
            cache_tech_dir_path = Path(self.cache_dir)
            dest_path = os.path.join(cache_tech_dir_path, os.path.basename(deck.path))
            with open(source_path, "r") as sf:
                with open(dest_path, "w") as df:
                    self.logger.info(
                        "Modifying LVS deck: {} -> {}".format(source_path, dest_path)
                    )
                    df.write(matcher.sub("", sf.read()))
                    df.write(LVS_DECK_INSERT_LINES)
        return True

    def setup_cdl(self) -> None:
        """Copy and hack the cdl, replacing pfet_01v8_hvt/nfet_01v8 with
        respective names in LVS deck
        """
        setting_dir = self.get_setting("technology.sky130.sky130A")
        setting_dir = Path(setting_dir)
        source_path = (
            setting_dir
            / "libs.ref"
            / self.library_name
            / "cdl"
            / f"{self.library_name}.cdl"
        )
        if not source_path.exists():
            raise FileNotFoundError(f"CDL not found: {source_path}")

        cache_tech_dir_path = Path(self.cache_dir)
        os.makedirs(cache_tech_dir_path, exist_ok=True)
        dest_path = cache_tech_dir_path / f"{self.library_name}.cdl"

        # device names expected in LVS decks
        if self.get_setting("vlsi.core.lvs_tool") == "hammer.lvs.calibre":
            pmos = "phighvt"
            nmos = "nshort"
        elif self.get_setting("vlsi.core.lvs_tool") == "hammer.lvs.pegasus":
            pmos = "pfet_01v8_hvt"
            nmos = "nfet_01v8"
        elif self.get_setting("vlsi.core.lvs_tool") == "hammer.lvs.netgen":
            pmos = "sky130_fd_pr__pfet_01v8_hvt"
            nmos = "sky130_fd_pr__nfet_01v8"
        else:
            shutil.copy2(source_path, dest_path)
            return

        with open(source_path, "r") as sf:
            with open(dest_path, "w") as df:
                self.logger.info(
                    "Modifying CDL netlist: {} -> {}".format(source_path, dest_path)
                )
                df.write("*.SCALE MICRON\n")
                for line in sf:
                    line = line.replace("pfet_01v8_hvt", pmos)
                    line = line.replace("nfet_01v8", nmos)
                    df.write(line)

    # Copy and hack the verilog
    #   - <library_name>.v: remove 'wire 1' and one endif line to fix syntax errors
    #   - primitives.v: set default nettype to 'wire' instead of 'none'
    #           (the open-source RTL sim tools don't treat undeclared signals as errors)
    #   - Deal with numerous inconsistencies in timing specify blocks.
    def setup_verilog(self) -> None:
        setting_dir = self.get_setting("technology.sky130.sky130A")
        setting_dir = Path(setting_dir)

        # <library_name>.v
        source_path = (
            setting_dir
            / "libs.ref"
            / self.library_name
            / "verilog"
            / f"{self.library_name}.v"
        )
        if not source_path.exists():
            raise FileNotFoundError(f"Verilog not found: {source_path}")

        cache_tech_dir_path = Path(self.cache_dir)
        os.makedirs(cache_tech_dir_path, exist_ok=True)
        dest_path = cache_tech_dir_path / f"{self.library_name}.v"

        with open(source_path, "r") as sf:
            with open(dest_path, "w") as df:
                self.logger.info(
                    "Modifying Verilog netlist: {} -> {}".format(source_path, dest_path)
                )
                for line in sf:
                    line = line.replace("wire 1", "// wire 1")
                    line = line.replace(
                        "`endif SKY130_FD_SC_HD__LPFLOW_BLEEDER_FUNCTIONAL_V",
                        "`endif // SKY130_FD_SC_HD__LPFLOW_BLEEDER_FUNCTIONAL_V",
                    )
                    df.write(line)

        # primitives.v
        source_path = (
            setting_dir / "libs.ref" / self.library_name / "verilog" / "primitives.v"
        )
        if not source_path.exists():
            raise FileNotFoundError(f"Verilog not found: {source_path}")

        cache_tech_dir_path = Path(self.cache_dir)
        os.makedirs(cache_tech_dir_path, exist_ok=True)
        dest_path = cache_tech_dir_path / "primitives.v"

        with open(source_path, "r") as sf:
            with open(dest_path, "w") as df:
                self.logger.info(
                    "Modifying Verilog netlist: {} -> {}".format(source_path, dest_path)
                )
                for line in sf:
                    line = line.replace(
                        "`default_nettype none", "`default_nettype wire"
                    )
                    df.write(line)

    # Copy and hack the tech-lef, adding this very important `licon` section
    def setup_techlef(self) -> None:
        cache_tech_dir_path = Path(self.cache_dir)
        os.makedirs(cache_tech_dir_path, exist_ok=True)
        if self.get_setting("technology.sky130.stdcell_library") == "sky130_fd_sc_hd":
            setting_dir = self.get_setting("technology.sky130.sky130A")
            setting_dir = Path(setting_dir)
            source_path = (
                setting_dir
                / "libs.ref"
                / self.library_name
                / "techlef"
                / f"{self.library_name}__nom.tlef"
            )
            dest_path = cache_tech_dir_path / f"{self.library_name}__nom.tlef"
        else:
            setting_dir = self.get_setting("technology.sky130.sky130_scl")
            setting_dir = Path(setting_dir)
            source_path = setting_dir / "sky130_scl_9T_tech" /  "lef" / "sky130_scl_9T.tlef"
            dest_path = cache_tech_dir_path / "sky130_scl_9T.tlef"
        if not source_path.exists():
            raise FileNotFoundError(f"Tech-LEF not found: {source_path}")

        with open(source_path, "r") as sf:
            with open(dest_path, "w") as df:
                self.logger.info(
                    "Modifying Technology LEF: {} -> {}".format(source_path, dest_path)
                )
                for line in sf:
                    df.write(line)
                    if (
                        self.get_setting("technology.sky130.stdcell_library")
                        == "sky130_scl"
                    ):
                        if line.strip() == "END poly":
                            df.write(_additional_tlef_edit_for_scl + _the_tlef_edit)
                    else:
                        if line.strip() == "END pwell":
                            df.write(_the_tlef_edit)

    def get_tech_power_hooks(self, tool_name: str) -> List[HammerToolHookAction]:
        hooks = {}

        def enable_scl_clk_gating_cell_hook(ht: HammerTool) -> bool:
            ht.append(
                "set_db [get_db lib_cells -if {.base_name == ICGX1}] .avoid false"
            )
            return True

        # The clock gating cell is set to don't touch/use in the cadence pdk (as of v0.0.3), work around that
        if self.get_setting("technology.sky130.stdcell_library") == "sky130_scl":
            hooks["joules"] = [
                HammerTool.make_pre_insertion_hook(
                    "synthesize_design", enable_scl_clk_gating_cell_hook
                )
            ]

        return hooks.get(tool_name, [])

    def get_tech_syn_hooks(self, tool_name: str) -> List[HammerToolHookAction]:
        hooks = {}

        def enable_scl_clk_gating_cell_hook(ht: HammerTool) -> bool:
            ht.append(
                "set_db [get_db lib_cells -if {.base_name == ICGX1}] .avoid false"
            )
            return True

        # The clock gating cell is set to don't touch/use in the cadence pdk (as of v0.0.3), work around that
        if self.get_setting("technology.sky130.stdcell_library") == "sky130_scl":
            hooks["genus"] = [
                HammerTool.make_pre_insertion_hook(
                    "syn_generic", enable_scl_clk_gating_cell_hook
                )
            ]

            # seems to mess up lvs for now
        # hooks['genus'].append(HammerTool.make_removal_hook("add_tieoffs"))
        return hooks.get(tool_name, [])

    # Power pins for clamps must be CLASS CORE
    def setup_io_lefs(self) -> None:
        sky130A_path = Path(self.get_setting("technology.sky130.sky130A"))
        source_path = (
            sky130A_path / "libs.ref" / "sky130_fd_io" / "lef" / "sky130_ef_io.lef"
        )
        if not source_path.exists():
            raise FileNotFoundError(f"IO LEF not found: {source_path}")

        cache_tech_dir_path = Path(self.cache_dir)
        os.makedirs(cache_tech_dir_path, exist_ok=True)
        dest_path = cache_tech_dir_path / "sky130_ef_io.lef"

        with open(source_path, "r") as sf:
            with open(dest_path, "w") as df:
                self.logger.info(
                    "Modifying IO LEF: {} -> {}".format(source_path, dest_path)
                )
                sl = sf.readlines()
                for net in ["VCCD1", "VSSD1", "VDDA", "VSSA", "VSSIO"]:
                    start = [idx for idx, line in enumerate(sl) if "PIN " + net in line]
                    end = [idx for idx, line in enumerate(sl) if "END " + net in line]
                    intervals = zip(start, end)
                    for intv in intervals:
                        port_idx = [
                            idx
                            for idx, line in enumerate(sl[intv[0] : intv[1]])
                            if "PORT" in line and "met3" in sl[intv[0] + idx + 1]
                        ]
                        for idx in port_idx:
                            sl[intv[0] + idx] = sl[intv[0] + idx].replace(
                                "PORT", "PORT\n      CLASS CORE ;"
                            )
                # force class to spacer
                # TODO: the disconnect_* slices are also broken like this, but we're not using them
                start = [
                    idx
                    for idx, line in enumerate(sl)
                    if "MACRO sky130_ef_io__connect_vcchib_vccd_and_vswitch_vddio_slice_20um"
                    in line
                ]
                sl[start[0] + 1] = sl[start[0] + 1].replace("AREAIO", "SPACER")

                for idx, line in enumerate(sl):
                    if "PIN OUT" in line:
                        sl[idx + 1].replace(
                            "DIRECTION INPUT ;",
                            "DIRECTION INPUT ;\n    ANTENNAGATEAREA 1.529 LAYER met3 ;",
                        )

                df.writelines(sl)

    def setup_hvl_ls_lef(self) -> bool:
        # Treat HVL cells as if they were hard macros to avoid needing to set them
        # up "properly" with multiple power domains

        lef_name = "sky130_fd_sc_hvl__lsbufhv2lv_1.lef"

        sky130A_path = Path(self.get_setting("technology.sky130.sky130A"))
        source_path = (
            sky130A_path
            / "libs.ref"
            / "sky130_fd_sc_hvl"
            / "lef"
            / "sky130_fd_sc_hvl.lef"
        )
        cache_path = Path(self.cache_dir) / "fd_sc_hvl__lef" / lef_name
        cache_path.parent.mkdir(exist_ok=True)

        with source_path.open("r") as sf, cache_path.open("w") as df:
            self.logger.info(f"Patching HVL LS LEF: {source_path} -> {cache_path}")
            is_in_site_def = False
            is_in_macro_def = False
            for line in sf:
                if is_in_site_def:
                    if "END unithv" in line:
                        is_in_site_def = False
                elif not is_in_macro_def and "SITE unithv" in line:
                    is_in_site_def = True
                elif "MACRO " in line:
                    is_in_macro_def = True
                    df.write(line)
                elif "SITE unithv" in line:
                    pass
                else:
                    df.write(
                        line.replace("CLASS CORE", "CLASS BLOCK")
                        if not (("ANTENNACELL" in line) or ("SPACER" in line))
                        else line
                    )

    def get_tech_par_hooks(self, tool_name: str) -> List[HammerToolHookAction]:
        hooks = {
            "innovus": [
                HammerTool.make_post_insertion_hook(
                    "init_design", sky130_innovus_settings
                ),
                HammerTool.make_pre_insertion_hook("power_straps", sky130_connect_nets),
                HammerTool.make_pre_insertion_hook(
                    "write_design", sky130_connect_nets2
                ),
            ]
        }
        # there are no cap/decap cells in the cadence stdcell library as of version 0.0.3, so we can't do things that reference them
        if self.get_setting("technology.sky130.stdcell_library") == "sky130_scl":
            hooks["innovus"].extend(
                [
                    HammerTool.make_pre_insertion_hook(
                        "power_straps", power_rail_straps_no_tapcells
                    ),
                    HammerTool.make_pre_insertion_hook(
                        "clock_tree", set_cts_base_cells
                    ),
                ]
            )
        else:
            hooks["innovus"].append(
                HammerTool.make_pre_insertion_hook(
                    "place_tap_cells", sky130_add_endcaps
                )
            )

        return hooks.get(tool_name, [])

    def get_tech_drc_hooks(self, tool_name: str) -> List[HammerToolHookAction]:
        calibre_hooks = []
        pegasus_hooks = []
        if self.get_setting("technology.sky130.drc_blackbox_srams"):
            calibre_hooks.append(
                HammerTool.make_post_insertion_hook(
                    "generate_drc_run_file", calibre_drc_blackbox_srams
                )
            )
            pegasus_hooks.append(
                HammerTool.make_post_insertion_hook(
                    "generate_drc_ctl_file", pegasus_drc_blackbox_srams
                )
            )
        pegasus_hooks.append(
            HammerTool.make_post_insertion_hook(
                "generate_drc_ctl_file", pegasus_drc_blackbox_io_cells
            )
        )
        pegasus_hooks.append(
            HammerTool.make_post_insertion_hook(
                "generate_drc_ctl_file", false_rules_off
            )
        )
        hooks = {"calibre": calibre_hooks, "pegasus": pegasus_hooks}
        return hooks.get(tool_name, [])

    def get_tech_lvs_hooks(self, tool_name: str) -> List[HammerToolHookAction]:
        calibre_hooks = []
        pegasus_hooks = []
        if self.use_sram22:
            calibre_hooks.append(
                HammerTool.make_post_insertion_hook(
                    "generate_lvs_run_file", sram22_lvs_recognize_gates_all
                )
            )
        if self.get_setting("technology.sky130.lvs_blackbox_srams"):
            calibre_hooks.append(
                HammerTool.make_post_insertion_hook(
                    "generate_lvs_run_file", calibre_lvs_blackbox_srams
                )
            )
            pegasus_hooks.append(
                HammerTool.make_post_insertion_hook(
                    "generate_lvs_ctl_file", pegasus_lvs_blackbox_srams
                )
            )

        if self.get_setting("technology.sky130.stdcell_library") == "sky130_scl":
            pegasus_hooks.append(
                HammerTool.make_post_insertion_hook(
                    "generate_lvs_ctl_file", pegasus_lvs_add_130a_primitives
                )
            )
        hooks = {"calibre": calibre_hooks, "pegasus": pegasus_hooks}
        return hooks.get(tool_name, [])

    @staticmethod
    def openram_sram_names() -> List[str]:
        """Return a list of cell-names of the OpenRAM SRAMs (that we'll use)."""
        return [
            "sky130_sram_1kbyte_1rw1r_32x256_8",
            "sky130_sram_1kbyte_1rw1r_8x1024_8",
            "sky130_sram_2kbyte_1rw1r_32x512_8",
        ]

    @staticmethod
    def sky130_sram_primitive_names() -> List[str]:
        spice_filenames = [
            "sky130_fd_pr__pfet_01v8",
            "sky130_fd_pr__nfet_01v8",
            "sky130_fd_pr__pfet_01v8_hvt",
            "sky130_fd_pr__special_nfet_latch",
            "sky130_fd_pr__special_nfet_pass",
        ]  # "sky130_fd_pr__special_pfet_latch", ]
        paths = []
        for fname in spice_filenames:
            paths.append(
                f"""/tools/commercial/skywater/local/sky130A/libs.ref/sky130_fd_pr/spice/{
                    fname
                }.pm3.spice"""
            )
        # TODO: this is bc line 535 in the bwrc one causes a syntax error
        paths.append(
            "/tools/C/elamdf/chipyard_dev/vlsi/sky130_fd_pr__special_pfet_latch.pm3.spice"
        )
        return paths

    @staticmethod
    def sky130_sram_names() -> List[str]:
        sky130_sram_names = []
        sram_cache_json = (
            importlib.resources.files("hammer.technology.sky130")
            .joinpath("sram-cache.json")
            .read_text()
        )
        dl = json.loads(sram_cache_json)
        for d in dl:
            sky130_sram_names.append(d["name"])
        return sky130_sram_names


# string constants
# the io libs (sky130a)
_the_tlef_edit = """
LAYER AREAIDLD
  TYPE MASTERSLICE ;
END AREAIDLD

LAYER licon
  TYPE CUT ;
END licon
"""
_additional_tlef_edit_for_scl = """
LAYER nwell
  TYPE MASTERSLICE ;
END nwell
LAYER pwell
  TYPE MASTERSLICE ;
END pwell
LAYER li1
  TYPE MASTERSLICE ;
END li1
"""

LVS_DECK_INSERT_LINES = """
LVS FILTER D  OPEN  SOURCE
LVS FILTER D  OPEN  LAYOUT
"""

LVS_DECK_SCRUB_LINES = [
    "VIRTUAL CONNECT REPORT",
    "SOURCE PRIMARY",
    "SOURCE SYSTEM SPICE",
    "SOURCE PATH",
    "ERC",
    "LVS REPORT",
]


# various Innovus database settings
def sky130_innovus_settings(ht: HammerTool) -> bool:
    assert isinstance(ht, HammerPlaceAndRouteTool), "Innovus settings only for par"
    assert isinstance(ht, TCLTool), "innovus settings can only run on TCL tools"
    """Settings for every tool invocation"""
    ht.append(
        f"""

##########################################################
# Placement attributes  [get_db -category place]
##########################################################
#-------------------------------------------------------------------------------
set_db place_global_place_io_pins  true

set_db opt_honor_fences true
set_db place_detail_dpt_flow true
set_db place_detail_color_aware_legal true
set_db place_global_solver_effort high
set_db place_detail_check_cut_spacing true
set_db place_global_cong_effort high
set_db add_fillers_with_drc false

##########################################################
# Optimization attributes  [get_db -category opt]
##########################################################
#-------------------------------------------------------------------------------

set_db opt_fix_fanout_load true
set_db opt_clock_gate_aware false
set_db opt_area_recovery true
set_db opt_post_route_area_reclaim setup_aware
set_db opt_fix_hold_verbose true

##########################################################
# Clock attributes  [get_db -category cts]
##########################################################
#-------------------------------------------------------------------------------
set_db cts_target_skew 0.03
set_db cts_max_fanout 10
#set_db cts_target_max_transition_time .3
set_db opt_setup_target_slack 0.10
set_db opt_hold_target_slack 0.10

##########################################################
# Routing attributes  [get_db -category route]
##########################################################
#-------------------------------------------------------------------------------
set_db route_design_antenna_diode_insertion 1
set_db route_design_antenna_cell_name "{"sky130_fd_sc_hd__diode_2" if ht.get_setting("technology.sky130.stdcell_library") == "sky130_fd_sc_hd" else "ANTENNA"}"

set_db route_design_high_freq_search_repair true
set_db route_design_detail_post_route_spread_wire true
set_db route_design_with_si_driven true
set_db route_design_with_timing_driven true
set_db route_design_concurrent_minimize_via_count_effort high
set_db opt_consider_routing_congestion true
set_db route_design_detail_use_multi_cut_via_effort medium
    """
    )
    if ht.hierarchical_mode in {HierarchicalMode.Top, HierarchicalMode.Flat}:
        ht.append(
            """
# For top module: snap die to manufacturing grid, not placement grid
set_db floorplan_snap_die_grid manufacturing
        """
        )

        # ht.append(
        #     """
    # # note this is required for sky130_fd_sc_hd, the design has a ton of drcs if bottom layer is 1
    # # TODO: why is setting routing_layer not enough?
    # set_db design_bottom_routing_layer 2
    # set_db design_top_routing_layer 6
    # # deprected syntax, but this used to always work
    # set_db route_design_bottom_routing_layer 2
    #           """
    # )

    return True


def sky130_connect_nets(ht: HammerTool) -> bool:
    assert isinstance(ht, HammerPlaceAndRouteTool), "connect global nets only for par"
    assert isinstance(ht, TCLTool), "connect global nets can only run on TCL tools"
    for pwr_gnd_net in ht.get_all_power_nets() + ht.get_all_ground_nets():
        if pwr_gnd_net.tie is not None:
            ht.append(
                "connect_global_net {tie} -type pg_pin -pin_base_name {net} -all -auto_tie -netlist_override".format(
                    tie=pwr_gnd_net.tie, net=pwr_gnd_net.name
                )
            )
            ht.append(
                "connect_global_net {tie} -type net    -net_base_name {net} -all -netlist_override".format(
                    tie=pwr_gnd_net.tie, net=pwr_gnd_net.name
                )
            )
    return True


# Pair VDD/VPWR and VSS/VGND nets
#   these commands are already added in Innovus.write_netlist,
#   but must also occur before power straps are placed
def sky130_connect_nets2(ht: HammerTool) -> bool:
    sky130_connect_nets(ht)
    return True


def sky130_add_endcaps(ht: HammerTool) -> bool:
    assert isinstance(ht, HammerPlaceAndRouteTool), "endcap insertion only for par"
    assert isinstance(ht, TCLTool), "endcap insertion can only run on TCL tools"
    endcap_cells = ht.technology.get_special_cell_by_type(CellType.EndCap)
    endcap_cell = endcap_cells[0].name[0]
    ht.append(
        f"""
set_db add_endcaps_boundary_tap     true
set_db add_endcaps_left_edge        {endcap_cell}
set_db add_endcaps_right_edge       {endcap_cell}
add_endcaps
    """
    )
    return True


# this needs to only be emitted in innovus, since it breaks the genus flow with the complaint that there are no usable inverters/logic cells (???) in version 211
def set_cts_base_cells(ht: HammerTool) -> bool:
    ht.append(
        """
set_db cts_buffer_cells {CLKBUFX2 CLKBUFX4 CLKBUFX8}
set_db cts_clock_gating_cells {ICGX1}
              """
    )
    return True


def power_rail_straps_no_tapcells(ht: HammerTool) -> bool:
    assert not ht.get_setting(
        "par.generate_power_straps_options.by_tracks.generate_rail_layer"
    ), """
Rails must be placed by this hook for sky130_scl!
Set par.generate_power_straps_options.by_tracks.generate_rail_layer: false"""
    #  We do this since there are no explicit tapcells in sky130_scl
    # just need the rail ones, others are placed as usual.
    ht.append(
        """
# Power strap definition for layer met1 (rails):
# should be .14
set_db add_stripes_stacked_via_top_layer met1
set_db add_stripes_stacked_via_bottom_layer met1
set_db add_stripes_spacing_from_block 4.000
add_stripes -nets {VDD VSS} -layer met1 -direction horizontal -start_offset -.2 -width .4 -spacing 3.74 -set_to_set_distance 8.28 -start_from bottom -switch_layer_over_obs false -max_same_layer_jog_length 2 -pad_core_ring_top_layer_limit met5 -pad_core_ring_bottom_layer_limit met1 -block_ring_top_layer_limit met5 -block_ring_bottom_layer_limit met1 -use_wire_group 0 -snap_wire_center_to_grid none
"""
    )
    return True


def calibre_drc_blackbox_srams(ht: HammerTool) -> bool:
    assert isinstance(ht, HammerDRCTool), "Exlude SRAMs only in DRC"
    drc_box = ""
    for name in SKY130Tech.sky130_sram_names():
        drc_box += f"\nEXCLUDE CELL {name}"
    run_file = ht.drc_run_file  # type: ignore
    with open(run_file, "a") as f:
        f.write(drc_box)
    return True


# pegasus won't be able to drc the sky130a ios
def pegasus_drc_blackbox_io_cells(ht: HammerTool) -> bool:
    assert isinstance(ht, HammerDRCTool) and ht.tool_config_prefix() == "drc.pegasus", (
        "Exlude IOs only for Pegasus DRC"
    )
    drc_box = ""
    io_cell_names = [
        "sky130_ef_io__*"
    ]  # TODO i don't think epgasus actually recognizes these?
    # io_cell_names = ["sky130_ef_io__gpiov2_pad_wrapped"]
    for name in io_cell_names:
        drc_box += f"\nexclude_cell {name}"
    run_file = ht.drc_ctl_file  # type: ignore
    with open(run_file, "a") as f:
        f.write(drc_box)
    return True

def false_rules_off(x: HammerTool) -> bool:
    # in sky130_rev_0.0_2.3 rules, cadence included a .cfg to turn off false rules - this typically has to be loaded via the GUI but this step hacks the flags into pegasusdrcctl and turns the false rules off
    # if FALSEOFF is defined, the rules are turned off
    # docs for hooks: /scratch/ee198-20-aaf/barduino-ofot/vlsi/hammer/hammer/drc/pegasus/README.md

    # Version 0.0_2.7  June 13, 2025
    drc_box = """
//=== Configurator controls ===
#DEFINE FALSEOFF
//      (these rules are on by default and may produce false violations)
#DEFINE SRAM
// These are the affected rules and alternate implementation: 
//    licon.4a: licon in areaid.ce must overlap LI
//    licon.4b: licon in areaid.ce must overlap (poly or diff or tap)
//    mcon.cover.1: mcon in areaid.ce must overlap LI
// ** WARNING: This switch is for debugging purposes only, errors may be missed **
#DEFINE NODEN
//  Recommended Rules (RC,RR) & Guidelines (NC)
#UNDEFINE RC
#UNDEFINE NC
//  Optional Switches 
#UNDEFINE frontend
#UNDEFINE backend
//=== End of configurator controls ===
    """
    run_file = x.drc_ctl_file  # type: ignore
    with open(run_file, "a") as f:
        f.write(drc_box)
    return True

def pegasus_drc_blackbox_srams(ht: HammerTool) -> bool:
    assert isinstance(ht, HammerDRCTool), "Exlude SRAMs only in DRC"
    drc_box = ""
    for name in SKY130Tech.sky130_sram_names():
        drc_box += f"\nexclude_cell {name}"
    run_file = ht.drc_ctl_file  # type: ignore
    with open(run_file, "a") as f:
        f.write(drc_box)
    return True


def calibre_lvs_blackbox_srams(ht: HammerTool) -> bool:
    assert isinstance(ht, HammerLVSTool), "Blackbox and filter SRAMs only in LVS"
    lvs_box = ""
    for name in SKY130Tech.sky130_sram_names():
        lvs_box += f"\nLVS BOX {name}"
        lvs_box += f"\nLVS FILTER {name} OPEN "
    run_file = ht.lvs_run_file  # type: ignore
    with open(run_file, "a") as f:
        f.write(lvs_box)
    return True


# required for sram22 since they use the 130a primiviites
def pegasus_lvs_add_130a_primitives(ht: HammerTool) -> bool:
    return True
    assert isinstance(ht, HammerLVSTool), "Blackbox and filter SRAMs only in LVS"
    lvs_box = ""
    for name in SKY130Tech.sky130_sram_primitive_names():
        lvs_box += f"""\nschematic_path "{name}" spice;"""
    # this is because otherwise lvs crashes with tons of stdcell-level pin mismatches
    lvs_box += """\nlvs_inconsistent_reduction_threshold -none;"""
    run_file = ht.lvs_ctl_file  # type: ignore
    with open(run_file, "r+") as f:
        # Remove SRAM SPICE file includes.
        pattern = "schematic_path.*({}).*spice;\n".format(
            "|".join(SKY130Tech.sky130_sram_primitive_names())
        )
        matcher = re.compile(pattern)
        contents = f.read()
        fixed_contents = contents + lvs_box
        f.seek(0)
        f.write(fixed_contents)
    return True


def pegasus_lvs_blackbox_srams(ht: HammerTool) -> bool:
    assert isinstance(ht, HammerLVSTool), "Blackbox and filter SRAMs only in LVS"
    lvs_box = ""
    for (
        name
        # + SKY130Tech.sky130_sram_primitive_names()
    ) in SKY130Tech.sky130_sram_names():
        lvs_box += f"\nlvs_black_box {name} -gray"
    run_file = ht.lvs_ctl_file  # type: ignore
    with open(run_file, "r+") as f:
        # Remove SRAM SPICE file includes.
        pattern = "schematic_path.*({}).*spice;\n".format(
            "|".join(SKY130Tech.sky130_sram_names())
        )
        matcher = re.compile(pattern)
        contents = f.read()
        fixed_contents = matcher.sub("", contents) + lvs_box
        f.seek(0)
        f.write(fixed_contents)
    return True


def sram22_lvs_recognize_gates_all(ht: HammerTool) -> bool:
    assert isinstance(ht, HammerLVSTool), (
        "Change 'LVS RECOGNIZE GATES' from 'NONE' to 'ALL' for SRAM22"
    )
    run_file = ht.lvs_run_file  # type: ignore
    with open(run_file, "a") as f:
        f.write("\nLVS RECOGNIZE GATES ALL")
    return True


tech = SKY130Tech()
