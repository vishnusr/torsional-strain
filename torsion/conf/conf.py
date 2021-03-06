import random
import logging
import sys

from openeye import oechem, oeomega
from openeye import oeszybki

from .rotors import isRotatableBond, distance_predicate
from torsion.utils import get_sd_data, has_sd_data

MAX_CONFS = 100  # maximum conformers generated by Omega


def configure_omega(
    library, rotor_predicate, rms_cutoff, energy_window, num_conformers=MAX_CONFS
):
    opts = oeomega.OEOmegaOptions(oeomega.OEOmegaSampling_Dense)
    opts.SetEnumRing(False)
    opts.SetEnumNitrogen(oeomega.OENitrogenEnumeration_Off)
    opts.SetSampleHydrogens(True)
    opts.SetRotorPredicate(rotor_predicate)
    opts.SetIncludeInput(False)

    opts.SetEnergyWindow(energy_window)
    opts.SetMaxConfs(num_conformers)
    opts.SetRMSThreshold(rms_cutoff)

    conf_sampler = oeomega.OEOmega(opts)
    torlib = conf_sampler.GetTorLib()

    # torlib.ClearTorsionLibrary()
    for rule in library:
        if not torlib.AddTorsionRule(rule):
            oechem.OEThrow.Fatal("Failed to add torsion rule: {}".format(rule))
    conf_sampler.SetTorLib(torlib)

    return conf_sampler


def gen_starting_confs(
    mol,
    torsion_library,
    max_one_bond_away=True,
    num_conformers=MAX_CONFS,
    rms_cutoff=0.0,
    energy_window=25,
):

    # Identify the atoms in the dihedral
    TAGNAME = "TORSION_ATOMS_FRAGMENT"
    if not has_sd_data(mol, TAGNAME):
        raise ValueError("Molecule does not have the SD Data Tag '{}'.".format(TAGNAME))

    dihedralAtomIndices = [int(x) - 1 for x in get_sd_data(mol, TAGNAME).split()]
    inDih = oechem.OEOrAtom(
        oechem.OEOrAtom(
            oechem.OEHasAtomIdx(dihedralAtomIndices[0]),
            oechem.OEHasAtomIdx(dihedralAtomIndices[1]),
        ),
        oechem.OEOrAtom(
            oechem.OEHasAtomIdx(dihedralAtomIndices[2]),
            oechem.OEHasAtomIdx(dihedralAtomIndices[3]),
        ),
    )

    mol1 = mol.CreateCopy()
    mc_mol = oechem.OEMol(mol1)

    # Tag torsion atoms with their dihedral index
    for atom in mc_mol.GetAtoms():
        if atom.GetIdx() == dihedralAtomIndices[0]:
            atom.SetData("dihidx", 0)
        if atom.GetIdx() == dihedralAtomIndices[1]:
            atom.SetData("dihidx", 1)
        if atom.GetIdx() == dihedralAtomIndices[2]:
            atom.SetData("dihidx", 2)
        if atom.GetIdx() == dihedralAtomIndices[3]:
            atom.SetData("dihidx", 3)

    if num_conformers > 1:
        # Set criterion for rotatable bond
        if False and max_one_bond_away:
            # this max function makes this seem potentially broken
            only_one_bond_away = distance_predicate(
                dihedralAtomIndices[1], dihedralAtomIndices[2]
            )
            rotor_predicate = oechem.OEAndBond(
                only_one_bond_away, oechem.PyBondPredicate(isRotatableBond)
            )
        elif False:
            # this ONLY samples special bonds & neglects "regualr" torsions
            rotor_predicate = oechem.PyBondPredicate(isRotatableBond)
        else:
            # try this more general sampling, but leave prior versions untouched
            rotor_predicate = oechem.OEOrBond(
                oechem.OEIsRotor(), oechem.PyBondPredicate(isRotatableBond)
            )

        # Initialize conformer generator and multi-conformer library
        conf_generator = configure_omega(
            torsion_library, rotor_predicate, rms_cutoff, energy_window, num_conformers
        )

        # Generator conformers
        if not conf_generator(mc_mol, inDih):
            raise ValueError("Conformers cannot be generated.")
        logging.debug(
            "Generated a total of %d conformers for %s.",
            mc_mol.NumConfs(),
            mol.GetTitle(),
        )

        # Reassign
        new_didx = [-1, -1, -1, -1]
        for atom in mc_mol.GetAtoms():
            if atom.HasData("dihidx"):
                new_didx[atom.GetData("dihidx")] = atom.GetIdx()
        oechem.OEClearSDData(mc_mol)
        oechem.OESetSDData(mc_mol, TAGNAME, " ".join(str(x + 1) for x in new_didx))
        oechem.OESetSDData(
            mc_mol,
            "TORSION_ATOMS_ParentMol",
            get_sd_data(mol, "TORSION_ATOMS_ParentMol"),
        )
        oechem.OESetSDData(
            mc_mol,
            "TORSION_ATOMPROP",
            f"cs1:0:1;1%{new_didx[0]+1}:1%{new_didx[1]+1}:1%{new_didx[2]+1}:1%{new_didx[3]+1}",
        )

    for conf_no, conf in enumerate(mc_mol.GetConfs()):
        conformer_label = (
            mol.GetTitle()
            + "_"
            + "_".join(get_sd_data(mol, "TORSION_ATOMS_ParentMol").split())
            + "_{:02d}".format(conf_no)
        )
        oechem.OESetSDData(conf, "CONFORMER_LABEL", conformer_label)
        conf.SetTitle(conformer_label)

    return mc_mol


def get_best_conf(mol, dih, num_points):
    """Drive the primary torsion in the molecule and select the lowest 
       energy conformer to represent each dihedral angle
    """
    delta = 360.0 / num_points
    angle_list = [2 * i * oechem.Pi / num_points for i in range(num_points)]

    dih_atoms = [x for x in dih.GetAtoms()]

    # Create new output OEMol
    title = mol.GetTitle()
    tor_mol = oechem.OEMol()

    opts = oeszybki.OETorsionScanOptions()
    opts.SetDelta(delta)
    opts.SetForceFieldType(oeszybki.OEForceFieldType_MMFF94)
    opts.SetSolvationType(oeszybki.OESolventModel_NoSolv)
    tmp_angle = 0.0
    tor = oechem.OETorsion(
        dih_atoms[0], dih_atoms[1], dih_atoms[2], dih_atoms[3], tmp_angle
    )

    oeszybki.OETorsionScan(tor_mol, mol, tor, opts)
    oechem.OECopySDData(tor_mol, mol)

    # if 0 and 360 sampled because of rounding
    if tor_mol.NumConfs() > num_points:
        for conf in tor_mol.GetConfs():
            continue
        tor_mol.DeleteConf(conf)

    for angle, conf in zip(angle_list, tor_mol.GetConfs()):
        angle_deg = int(round(angle * oechem.Rad2Deg))
        tor_mol.SetActive(conf)
        oechem.OESetTorsion(
            conf, dih_atoms[0], dih_atoms[1], dih_atoms[2], dih_atoms[3], angle
        )

        conf_name = title + "_{:02d}".format(conf.GetIdx())
        oechem.OESetSDData(conf, "CONFORMER_LABEL", conf_name)
        oechem.OESetSDData(conf, "TORSION_ANGLE", "{:.0f}".format(angle_deg))
        conf.SetDoubleData("TORSION_ANGLE", angle_deg)
        conf.SetTitle("{}: Angle {:.0f}".format(conf_name, angle_deg))

    return tor_mol


def gen_torsional_confs(mol, dih, num_points, include_input=False):
    """Drives the primary torsion in the molecule and generates labeled 
    torsional conformers.

    Inputs:
        mol - OEMol 
                must have a 'CONFORMER_LABEL' in the sd_data
    """
    angle_list = [2 * i * oechem.Pi / num_points for i in range(num_points)]

    dih_atoms = [x for x in dih.GetAtoms()]

    # Create new output OEMol
    torsion_conformers = oechem.OEMol(mol)

    if not include_input:
        torsion_conformers.DeleteConfs()

    for conf_id, conf in enumerate(mol.GetConfs()):
        conf_name = get_sd_data(conf, "CONFORMER_LABEL")
        for angle_idx, angle in enumerate(angle_list):
            angle_deg = int(round(angle * oechem.Rad2Deg))
            oechem.OESetTorsion(
                conf, dih_atoms[0], dih_atoms[1], dih_atoms[2], dih_atoms[3], angle
            )

            new_conf = torsion_conformers.NewConf(conf)
            new_conf.SetDimension(3)
            new_conf_name = conf_name + "_{:02d}".format(angle_idx)
            oechem.OESetSDData(new_conf, "CONFORMER_LABEL", new_conf_name)
            oechem.OESetSDData(new_conf, "TORSION_ANGLE", "{:.0f}".format(angle_deg))
            new_conf.SetDoubleData("TORSION_ANGLE", angle_deg)
            new_conf.SetTitle("{}: Angle {:.0f}".format(conf_name, angle_deg))

    return torsion_conformers


def split_confs(mol):
    for conf in mol.GetConfs():
        new_mol = oechem.OEMol(conf)
        oechem.OECopySDData(new_mol, mol)
        yield new_mol
