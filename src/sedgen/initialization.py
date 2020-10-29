import numpy as np
import numba as nb
from scipy.stats import truncnorm

import itertools
import time
import warnings
import copy

from sedgen import general as gen
from sedgen.discretization import Bins, BinsMatricesMixin, McgBreakPatternMixin
from sedgen.evolution import ModelEvolutionMixin


"""TODO:
    - Provide option to specify generated minerals based on a number
      instead of filling up a given volume. In the former case, the
      simulated volume attribute also has more meaning.
    - Learning rate should be set semi-automatically. Based on smallest
      mean crystal size perhaps?
    - Change intra_cb_p to a function so that smaller crystal sizes have
     a smaller chance of intra_cb breakage and bigger ones a higher
     chance.
    - Implement generalization of intra_cb breakage; instead of
    performing operation per selected mcg in certain bin, perform the
    operation on all selected mcg at same time. This can be done as the
    random location for intra_cb breakage stems from a discrete uniform
    distribution.
    - Would be nice to work with masked arrays for the bin arrays to
    mask negative values, but unfortunately numba does not yet provide
    support for masked arrays.
"""


class SedGen(Bins, BinsMatricesMixin, McgBreakPatternMixin,
             ModelEvolutionMixin):
    """Initializes a SedGen model based on fundamental properties of
    modal mineralogy, interfacial composition and crystal size
    statistics

    Parameters:
    -----------
    minerals : list
        Mineral classes to use in model; the order in which the minerals
        are specified here should be kept accros all other parameters
    parent_rock_volume : float
        Volume representing parent rock to fill with crystals at start
        of model
    modal_mineralogy : np.array
        Volumetric proportions of mineral classes at start of model
    csd_means : np.array
        Crystal size means of mineral classes
    csd_stds : np.array
        Crystal size standard deviations of mineral classes
    interfacial_composition : np.array (optional)
    learning_rate : int (optional)
        Amount of change used during determination of N crystals per
        mineral class; defaults to 1000
    timed : bool (optional)
        Show timings of various initialization steps; defaults to False
    n_timesteps : int
        Number of iterations for the for loop which executes the given
        weathering processes
    n_standard_cases : int (optional)
        Number of standard cases to calculate the interface location
        probabilties for; defaults to 2000
    intra_cb_p : list(float) (optional)
        List of probabilities [0, 1] to specify how many of
        mono-crystalline grains per size bin will be effected by
        intra-crystal breakage every timestep; defaults to [0.5] to use
        0.5 for all present mineral classes
    intra_cb_thresholds : list(float) (optional)
        List of intra-crystal breakage size thresholds of mineral
        classes to specify that under the given theshold, intra_crystal
        breakage will not effect the mono-crystalline grains anymore;
        defaults to [1/256] to use 1/256 for all mineral classes
    chem_weath_rates : list(float) (optional)
        List of chemical weathering rates of mineral rates specified as
        'mm/year'. This is scaled internally to be implemented in a
        relative manner; defaults to [0.01] to use 0.01 mm/yr for all
        mineral classes as chemical weathering rate.
    enable_interface_location_prob : bool (optional)
        If True, the location of an interface along a pcg, will have an
        effect on its probability of breakage during inter-crystal
        breakage. Interfaces towards the outside of an pcg are more
        likely to break than those on the inside; defaults to True.
    enable_multi_pcg_breakage : bool (optional)
        If True, during inter-crystal breakage a pcg may break in more
        than two new pcg/mcg grains. This option might speed up the
        model. By activating all interfaces weaker than the selected
        interfaces, this behavior might be accomplished.
    enable_pcg_selection : bool (optional)
        If True, a selection of pcgs is performed to determine which
        pcgs will be affected by inter-crystal breakage during one
        iteration of the weathering procedure. Larger volume pcgs will
        have a higher chance of being selected than smaller ones. If
        enabled, this option probably will slow down the model in
        general.
    """

    def __init__(self, minerals, parent_rock_volume, modal_mineralogy,
                 csd_means, csd_stds, interfacial_composition=None,
                 learning_rate=1000, timed=False,
                 n_timesteps=100, n_standard_cases=2000,
                 intra_cb_p=[0.5], intra_cb_thresholds=[1/256],
                 chem_weath_rates=[0.01], enable_interface_location_prob=True,
                 enable_multi_pcg_breakage=False, enable_pcg_selection=False):

        print("---SedGen model initialization started---\n")
        self.minerals = minerals
        self.n_minerals = len(self.minerals)
        self.parent_rock_volume = parent_rock_volume
        self.modal_mineralogy = modal_mineralogy
        self.csd_means = csd_means
        self.csd_stds = csd_stds
        self.learning_rate = learning_rate

        self.n_timesteps = n_timesteps
        self.n_standard_cases = n_standard_cases
        self.enable_interface_location_prob = enable_interface_location_prob
        self.enable_multi_pcg_breakage = enable_multi_pcg_breakage
        self.enable_pcg_selection = enable_pcg_selection

        # Create array of intra-crystal breakage probabilities
        self.intra_cb_p = self.mineral_property_setter(intra_cb_p)

        # Create array of intra-cyrstal breakage size thresholds
        self.intra_cb_thresholds = \
            self.mineral_property_setter(intra_cb_thresholds)

        # Create array of chemical weathering rates
        self.chem_weath_rates = \
            self.mineral_property_setter(chem_weath_rates)

        if self.enable_interface_location_prob:
            # Calculate interface_location_prob array for standard
            # configurations of pcgs so that they can be looked up later on
            # instead of being calculated ad hoc.
            self.standard_prob_loc_cases = \
                np.array([create_interface_location_prob(
                    np.arange(x)) for x in range(1, self.n_standard_cases+1)],
                    dtype=np.object)

        print("Initializing modal mineralogy...")
        # Assert that modal mineralogy proportions sum up to unity.
        assert np.isclose(np.sum(modal_mineralogy), 1.0), \
            "Modal mineralogy proportions do not sum to 1"

        # Divide parent rock volume over all mineral classes based on
        # modal mineralogy
        self.modal_volume = self.parent_rock_volume * self.modal_mineralogy

        print("Initializing csds...")
        self.csds = np.array([self.initialize_csd(m)
                              for m in range(self.n_minerals)])

        print("Initializing bins...")
        Bins.__init__(self)

        print("Simulating mineral occurences...", end=" ")
        if timed:
            tic0 = time.perf_counter()
        self.minerals_N, self.simulated_volume, crystal_sizes_per_mineral = \
            self.create_N_crystals()
        self.mass_balance_initial = np.sum(self.simulated_volume)

        self.N_crystals = np.sum(self.minerals_N)

        if timed:
            toc0 = time.perf_counter()
            print(f" Done in{toc0 - tic0: 1.4f} seconds")
        else:
            print("")

        print("Initializing interfaces...", end=" ")
        if timed:
            tic1 = time.perf_counter()
        self.interfaces = self.get_interface_labels()
        self.interfacial_composition = interfacial_composition

        self.number_proportions = self.calculate_number_proportions()
        self.interface_proportions = self.calculate_interface_proportions()
        self.interface_proportions_normalized = \
            self.calculate_interface_proportions_normalized()
        self.interface_frequencies = self.calculate_interface_frequencies()
        self.interface_frequencies = \
            self.perform_interface_frequencies_correction()

        transitions_per_mineral = \
            self.create_transitions_per_mineral_correctly()

        self.interface_array = \
            create_interface_array(self.minerals_N, transitions_per_mineral)
        # self.interface_pairs = gen.create_pairs(self.interface_array)
        if timed:
            toc1 = time.perf_counter()
            print(f" Done in{toc1 - tic1: 1.4f} seconds")
        else:
            print("")

        if timed:
            print("Counting interfaces...", end=" ")
            tic2 = time.perf_counter()
        else:
            print("Counting interfaces...")

        self.interface_counts_matrix = \
            gen.count_and_convert_interfaces_to_matrix(self.interface_array,
                                                       self.n_minerals)
        if timed:
            toc2 = time.perf_counter()
            print(f" Done in{toc2 - tic2: 1.4f} seconds")

        print("Correcting interface arrays for consistency...")
        self.interface_array, self.interface_counts_matrix = \
            self.perform_interface_array_correction()

        print("Initializing crystal size array...", end=" ")
        if timed:
            tic3 = time.perf_counter()
        self.minerals_N_actual = self.calculate_actual_minerals_N()

        self.crystal_size_array = \
            self.fill_main_cystal_size_array(crystal_sizes_per_mineral)
        if timed:
            toc3 = time.perf_counter()
            print(f" Done in{toc3 - tic3: 1.4f} seconds")
        else:
            print("")

        print("Initializing inter-crystal breakage probability arrays...")
        # ???
        # Probability arrays need to be normalized so that they carry
        # the same weight (importance) down the line. During
        # calculations with the probility arrays, weights might be added
        # as coefficients to change the importance of the different
        # arrays as required.
        # ???

        # The more an interface is located towards the outside of a
        # grain, the more chance it has to be broken.
        self.interface_location_prob = self.create_interface_location_prob()
        # The higher the strength of an interface, the less chance it
        # has to be broken.
        self.interface_strengths_prob = \
            gen.get_interface_strengths_prob(
                self.interface_proportions_normalized,
                self.interface_array)
        # The bigger an interface is, the more chance it has to be
        # broken.
        self.interface_size_prob = \
            gen.get_interface_size_prob(self.crystal_size_array)

        self.interface_constant_prob = \
            self.interface_size_prob / self.interface_strengths_prob

        print("Initializing model evolution arrays...")
        ModelEvolutionMixin.__init__(self)

        print("Initializing discretization for model's weathering...")
        # Initialize bins matrices for chemical weathering states
        BinsMatricesMixin.__init__(self)
        # Initialize discrete break patterns for use in intra_cb
        McgBreakPatternMixin.__init__(self)

        print("\n---SedGen model initialization finished succesfully---")

    def __repr__(self):
        output = f"SedGen({self.minerals}, {self.parent_rock_volume}, " \
                 f"{self.modal_mineralogy}, {self.csd_means}, " \
                 f"{self.csd_stds}, {self.interfacial_composition}, " \
                 f"{self.learning_rate}"
        return output

    def get_interface_labels(self):
        """Returns list of combinations of interfaces between provided
        list of minerals

        """

        interface_labels = \
            ["".join(pair) for pair in
             itertools.combinations_with_replacement(self.minerals, 2)]

        return interface_labels

    def initialize_csd(self, m, trunc_left=1/256, trunc_right=30):
        """Initalizes the truncated lognormal crystal size distribution

        Parameters:
        -----------
        m : int
            Number specifying mineral class
        trunc_left : float(optional)
            Value to truncate lognormal distribution to on left side,
            i.e. smallest values
        trunc_right : float(optional)
            Value to truncate lognormal distribution to on right side,
            i.e. biggest values

        Returns:
        --------
        csd : scipy.stats.truncnorm
            Truncated lognormal crystal size distribution
        """

        mean = np.log(self.csd_means[m])
        std = np.exp(self.csd_stds[m])

        if not np.isinf(trunc_left):
            trunc_left = np.log(trunc_left)

        if not np.isinf(trunc_right):
            trunc_right = np.log(trunc_right)

        a, b = (trunc_left - mean) / std, (trunc_right - mean) / std
        csd = truncnorm(loc=mean, scale=std, a=a, b=b)

        return csd

    def calculate_N_crystals(self, m):
        """Request crystals from CSD until accounted modal volume is
        filled.

        Idea: use pdf to speed up process --> This can only be done if
        the CSD is converted to a 'crystal volume distribution'.

        From this, the number of crystals per mineral class will be
        known while also giving the total number of crystals (N) in 1 m³
        of parent rock.
        """
        total_volume_mineral = 0
        requested_volume = self.modal_volume[m]
        crystals = []
        crystals_append = crystals.append
        crystals_total = 0
        # crystals_total_append = crystals_total.append

        rs = 0
        while total_volume_mineral < requested_volume:
            diff = requested_volume - total_volume_mineral
            crystals_requested = \
                int(diff / (self.modal_mineralogy[m] * self.learning_rate)) + 1

            crystals_total += crystals_requested
            crystals_to_add = \
                np.exp(self.csds[m].rvs(size=crystals_requested,
                                        random_state=rs))

            crystals_append(gen.calculate_volume_sphere(crystals_to_add))
            total_volume_mineral += np.sum(crystals[rs])

            rs += 1

        crystals_array = np.concatenate(crystals)

        crystals_binned = \
            (np.searchsorted(self.volume_bins,
                             crystals_array) - 1).astype(np.uint16)

        # Capture and correct crystals that fall outside
        # the leftmost bin as they end up as bin 0 but since 1 gets
        # subtracted from all bins they end up as the highest value
        # of np.uint16 as negative values are not possible
        crystals_binned[crystals_binned > self.n_bins] = 0

        return crystals_total, np.sum(crystals_total), \
            total_volume_mineral, crystals_binned

    def create_N_crystals(self):
        minerals_N = {}
        print("|", end="")
        for m, n in enumerate(self.minerals):
            print(n, end="|")
            minerals_N[n] = \
                self.calculate_N_crystals(m=m)
        minerals_N_total = np.array([N[1] for N in minerals_N.values()])
        simulated_volume = np.array([N[2] for N in minerals_N.values()])
        crystals = [N[3] for N in minerals_N.values()]

        return minerals_N_total, simulated_volume, crystals

    def calculate_number_proportions(self):
        """Returns number proportions"""
        return gen.normalize(self.minerals_N).reshape(-1, 1)

    # To Do: add alpha factor to function to handle non-random interfaces
    def calculate_interface_proportions(self):
        interface_proportions_pred = \
            self.number_proportions * self.number_proportions.T

        return interface_proportions_pred

    def calculate_interface_frequencies(self):
        interface_frequencies = \
            np.round(self.interface_proportions * (self.N_crystals - 1))\
              .astype(np.uint32)
        return interface_frequencies

    def calculate_interface_proportions_normalized(self):
        interface_proportions_normalized = \
            np.divide(self.interface_proportions,
                      np.sum(self.interface_proportions, axis=1).reshape(-1, 1)
                      )
        return interface_proportions_normalized

    def create_transitions_per_mineral_correctly(self, corr=5,
                                                 random_seed=911):
        """Correction 'corr' is implemented to obtain a bit more
        possibilities than needed to make sure there are enough values
        to fill the interface array later on."""
        transitions_per_mineral = []

        iterable = self.interface_frequencies.copy()

        prng = np.random.default_rng(random_seed)
        print("|", end="")
        for i, row in enumerate(iterable):
            print(self.minerals[i], end="|")
            N = self.minerals_N[i] + corr
            c = prng.random(size=N)
            transitions_per_mineral.append(
                create_transitions_correctly(row, c, N))

        return tuple(transitions_per_mineral)

    def perform_interface_frequencies_correction(self):
        interface_frequencies_corr = self.interface_frequencies.copy()
        diff = np.sum(self.interface_frequencies) - (self.N_crystals - 1)
        interface_frequencies_corr[0, 0] -= int(diff)

        return interface_frequencies_corr

    def perform_interface_array_correction(self):
        """Remove or add crystals from/to interface_array where
        necessary
        """
        interface_array_corr = self.interface_array.copy()
        prob_unit = 1
        # interface_pairs_corr = self.interface_pairs.copy()
        interface_frequencies_corr = self.interface_counts_matrix.copy()
        diff = [np.sum(self.interface_array == x) for x in range(6)] \
            - self.minerals_N
        # print("diff", diff)
        # print(interface_frequencies_corr)

        for index, item in enumerate(diff):
            if item > 0:
                print("too much", self.minerals[index], item)
                # Select exceeding number crystals from end of array
                for i in range(item):
                    # Select index to correct
                    corr_index = np.where(interface_array_corr == index)[0][-1]

                    # Add/remove interfaces to/from interface_frequencies_corr
                    try:
                        # Remove first old interface
                        pair_index = (interface_array_corr[corr_index-1],
                                      interface_array_corr[corr_index])
                        # print("old1", pair_index)
                        interface_frequencies_corr[pair_index] -= prob_unit
                        # Add newly formed interface
                        pair_index = (interface_array_corr[corr_index-1],
                                      interface_array_corr[corr_index+1])
                        # print("new", pair_index)
                        interface_frequencies_corr[pair_index] += prob_unit
                        # Remove second old interface
                        pair_index = (interface_array_corr[corr_index],
                                      interface_array_corr[corr_index+1])
                        # print("old2", pair_index)
                        interface_frequencies_corr[pair_index] -= prob_unit

                    except IndexError:
                        # print("interface removed from end of array")
                        pass
                        # IndexError might occur if correction takes place at
                        # very end of interface_array, as there is only one
                        # interface present there. Therefore, only the first
                        # old interface needs to be removed. Checking if we are
                        # at the start of the array should not be necessary as
                        # we always select corr_indices from the end of the
                        # interface_array.

                    # Delete crystals from interface_array_corr
                    interface_array_corr = \
                        np.delete(interface_array_corr, corr_index)

                    # print(interface_array_corr[-100:])
                    # print(interface_frequencies_corr)

            elif item < 0:
                print("too few", self.minerals[index], item)
                # Add newly formed interfaces to interface_frequencies_corr
                pair_index = (interface_array_corr[-1], index)
                interface_frequencies_corr[pair_index] += prob_unit
                # Add crystals to interface_array_corr
                interface_array_corr = \
                    np.concatenate((interface_array_corr,
                                    np.array([index] * -item, dtype=np.uint8)))
                # Add newly formed isomineral interfaces
                interface_frequencies_corr[index, index] += \
                    (-item - 1) * prob_unit
                # print(pair_index)
                # print(interface_array_corr[-100:])
                # print(interface_frequencies_corr)
            else:
                print("all good", self.minerals[index], item)
                pass

        return interface_array_corr, interface_frequencies_corr

    def create_interface_location_prob(self):
        """Creates an array descending and then ascending again to
        represent chance of inter crystal breakage of a poly crystalline
        grain (pcg).
        The outer interfaces have a higher chance of breakage than the
        inner ones based on their location within the pcg.
        This represents a linear function.
        Perhaps other functions might be added (spherical) to see the
        effects later on

        # Not worth it adding numba to this function
        """
        size, corr = divmod(self.interface_array.size, 2)
        ranger = np.arange(size, 0, -1, dtype=np.uint32)
        chance = np.append(ranger, ranger[-2+corr::-1])

        return chance

    def calculate_actual_minerals_N(self):
        minerals_N_total_actual = [np.sum(self.interface_array == i)
                                   for i in range(self.n_minerals)]
        return minerals_N_total_actual

    def fill_main_cystal_size_array(self, crystal_sizes_per_mineral):
        """After pre-generation of random crystal_sizes has been
        performed, the sizes are allocated according to the mineral
        order in the minerals/interfaces array
        """
        crystal_size_array = np.zeros(self.interface_array.shape,
                                      dtype=np.uint16)

        # Much faster way (6s) to create crystal size labels array than
        # to use modified function of interfaces array creating (1m40s)!
        print("|", end="")
        for i, mineral in enumerate(self.minerals):
            print(mineral, end="|")
            crystal_size_array[np.where(self.interface_array == i)] = \
                crystal_sizes_per_mineral[i]

        return crystal_size_array

    def calculate_actual_volumes(self):
        """Calculates the actual volume / modal mineralogy taken up by
        the crystal size array per mineral"""
        actual_volumes = []

        for m in range(self.n_minerals):
            # Get cystal size (binned) for mineral
            crystal_sizes = self.crystal_size_array[self.interface_array == m]
            # Convert bin labels to bin medians
            crystal_sizes_array = self.size_bins_medians[crystal_sizes]
            # Calculate sum of volume of crystal sizes and store result
            actual_volumes.append(
                np.sum(
                    gen.calculate_volume_sphere(
                        crystal_sizes_array)) / self.parent_rock_volume)

        return actual_volumes

    def check_properties(self):
        # Check that number of crystals per mineral in interface dstack
        # array equals the same number in minerals_N
        assert all([np.sum(self.interface_array == x) for x in range(6)]
                   - self.minerals_N == [0] * self.n_minerals), \
                   "N is not the same in interface_array and minerals_N"

        return "Number of crystals (N) is the same in interface_array and"
        "minerals_N"

    def weathering(self,
                   operations=["intra_cb",
                               "inter_cb",
                               "chem_mcg",
                               "chem_pcg"],
                   display_mass_balance=False,
                   display_mcg_sums=False,
                   timesteps=None,
                   timed=False,
                   inplace=False):

        # Whether to work on a copy of the instance or work 'inplace'
        self = self if inplace else copy.copy(self)

        if not timesteps:
            timesteps = self.n_timesteps

        mcg_broken = np.zeros_like(self.mcg)
        if timed:
            tac = time.perf_counter()
        # Start model
        for step in range(timesteps):
            # What timestep we're at
            if timed:
                tic = time.perf_counter()
            print(f"{step}/{self.n_timesteps}", end="\r", flush=True)

            # Perform weathering operations
            for operation in operations:
                if operation == "intra_cb":
                    # TODO: Insert check on timestep or n_mcg to
                    # perform intra_cb_breakage per mineral and per bin
                    # or in one operation for all bins and minerals.

                    # intra-crystal breakage
                    mcg_broken, residue, residue_count = \
                        self.intra_crystal_breakage_binned(alternator=step)
                    self.mcg = mcg_broken.copy()
                    # Add new mcg to mcg_chem_weath array to be able to
                    # use newly formed mcg during chemical weathering of
                    # mcg
                    if display_mcg_sums:
                        print("mcg sum over minerals after intra_cb but before"
                              "inter_cb",
                              np.sum(np.sum(self.mcg, axis=2), axis=0))
                    # Account for residue
                    self.residue[step] = residue
                    self.residue_count[step] = residue_count
                    if timed:
                        toc_intra_cb = time.perf_counter()

                elif operation == "inter_cb":
                    # inter-crystal breakage
                    self.pcgs_new, self.crystal_size_array_new,\
                        self.interface_constant_prob_new, \
                        self.pcg_chem_weath_array_new, self.mcg = \
                        self.inter_crystal_breakage(step)
                    if display_mcg_sums:
                        print("mcg sum after inter_cb",
                              np.sum(np.sum(self.mcg, axis=2), axis=0))
                    if timed:
                        toc_inter_cb = time.perf_counter()

                    # If no pcgs are remaining anymore, stop the model
                    if not self.pcgs_new:
                        print(f"After {step} steps all pcg have been broken"
                              "down to mcg")
                        return self.pcgs_new, self.mcg, self.pcg_additions, \
                            self.mcg_additions, self.pcg_comp_evolution, \
                            self.pcg_size_evolution, self.interface_counts_matrix, \
                            self.crystal_size_array_new, \
                            self.mcg_broken_additions, \
                            self.residue_additions, \
                            self.residue_count_additions, \
                            self.pcg_chem_residue_additions, \
                            self.mcg_chem_residue_additions, \
                            self.mass_balance, self.mcg_evolution

                # To Do: Provide option for different speeds of chemical
                # weathering per mineral class. This could be done by
                # moving to a different number of volume bins (n) per
                # mineral class. For the volume_perc_change this would
                # become: volume_perc_change = volume_perc_change ** n
                elif operation == "chem_mcg":
                    # chemical weathering of mcg
                    self.mcg, self.mcg_chem_residue = \
                        self.chemical_weathering_mcg()
                    if display_mcg_sums:
                        print("mcg sum after chem_mcg",
                              np.sum(np.sum(self.mcg, axis=2), axis=0))
                        print("mcg_chem_residue after chem_mcg",
                              self.mcg_chem_residue)
                    if timed:
                        toc_chem_mcg = time.perf_counter()

                elif operation == "chem_pcg":
                    # Don't perform chemical weathering of pcg in first
                    # timestep. Otherwise n_timesteps+1 bin arrays need
                    # to be initialized.
                    if step == 0:
                        toc_chem_pcg = time.perf_counter()
                        continue
                    # chemical weathering of pcg
                    self.pcgs_new, \
                        self.crystal_size_array_new, \
                        self.interface_constant_prob_new, \
                        self.pcg_chem_weath_array_new, \
                        self.pcg_chem_residue, \
                        self.interface_counts_matrix = \
                        self.chemical_weathering_pcg()
                    if timed:
                        toc_chem_pcg = time.perf_counter()

                else:
                    print(f"Warning: {operation} not recognized as a valid"
                          f"operation, skipping {operation} and continueing")
                    continue

            # Track model's evolution
            self.mcg_broken_additions[step] = \
                np.sum([np.sum(x) for x in mcg_broken])  # \
            # - np.sum(self.mcg_broken_additions)
            # self.residue_mcg_total += self.residue
            self.residue_additions[step] = self.residue[step]

            self.residue_count_additions[step] = \
                np.sum(self.residue_count) - \
                np.sum(self.residue_count_additions)

            self.pcg_additions[step] = len(self.pcgs_new)
            self.mcg_additions[step] = np.sum(self.mcg)  # - np.sum(mcg_additions)

            self.pcg_comp_evolution.append(self.pcgs_new)
            self.pcg_size_evolution.append(self.crystal_size_array_new)

            self.pcg_chem_residue_additions[step] = self.pcg_chem_residue
            self.mcg_chem_residue_additions[step] = self.mcg_chem_residue

            self.mcg_evolution[step] = np.sum(self.mcg, axis=0)

            # Mass balance check
            if display_mass_balance:
                vol_mcg = np.sum([self.volume_bins_medians_matrix * self.mcg])
                print("vol_mcg_total:", vol_mcg, "over",
                      np.sum(self.mcg), "mcg")
                vol_residue = \
                    np.sum(self.residue_additions) + \
                    np.sum(self.pcg_chem_residue_additions) + \
                    np.sum(self.mcg_chem_residue_additions)

                print("mcg_intra_cb_residue_total:",
                      np.sum(self.residue_additions))
                print("pcg_chem_residue_total:",
                      np.sum(self.pcg_chem_residue_additions))
                print("mcg_chem_residue_total:",
                      np.sum(self.mcg_chem_residue_additions))
                print("vol_residue_total:", vol_residue)

                vol_pcg = self.calculate_vol_pcg()
                print("vol_pcg_total:", vol_pcg, "over",
                      len(self.pcgs_new), "pcg")

                mass_balance = vol_pcg + vol_mcg + vol_residue
                self.mass_balance[step] = mass_balance
                print(f"new mass balance after step {step}: {mass_balance}\n")

            # If no pcgs are remaining anymore, stop the model
            if not self.pcgs_new:  # Faster to check if pcgs_new has any items
                print(f"After {step} steps all pcg have been broken down to"
                      "mcg")
                break

            if timed:
                if 'intra_cb' in operations:
                    print(f"Intra_cb {step} done"
                          f"in{toc_intra_cb - tic: 1.4f} seconds")
                if 'inter_cb' in operations:
                    print(f"Inter_cb {step} done"
                          f"in{toc_inter_cb - toc_intra_cb: 1.4f} seconds")
                if 'chem_mcg' in operations:
                    print(f"Chem_mcg {step} done"
                          f"in{toc_chem_mcg - toc_inter_cb: 1.4f} seconds")
                if 'chem_pcg' in operations:
                    print(f"Chem_pcg {step} done"
                          f"in{toc_chem_pcg - toc_chem_mcg: 1.4f} seconds")
                print("\n")

                toc = time.perf_counter()
                print(f"Step {step} done in{toc - tic: 1.4f} seconds")
                print(f"Time elapsed: {toc - tac} seconds\n")

        return self

    def calculate_vol_pcg(self):

        pcg_concat = np.concatenate(self.pcgs_new)
        csize_concat = np.concatenate(self.crystal_size_array_new)
        chem_concat = np.concatenate(self.pcg_chem_weath_array_new)

        vol_pcg = np.sum(self.volume_bins_medians_matrix[chem_concat,
                                                         pcg_concat,
                                                         csize_concat])

        return vol_pcg

    def inter_crystal_breakage(self, step):
        """Performs inter-crystal breakage where poly-crystalline grains
        will break on the boundary between two crystals.

        Parameters:
        -----------
        step : int
            ith iteration of the model (timestep number)

        Returns:
        --------
        pcgs_new : list of np.array(uint8)
            Newly formed list of poly-crystalline grains which are
            represented as seperate numpy arrays
        crystal_size_array_new: list of np.array(uint16)
            Newly formed list of the crystal sizes of the pcgs which are
            again represented by numpy arrays
        interface_constant_prob_new : list of np.array(float64)
            Newly formed list of the inter-crystal breakage
            probabilities for the present interfaces between crystals in
            seperate pcgs again represented by arrays
        mcg_new : np.array(uint32)
            Newly formed mono-crystalline grains during inter-crystal
            breakage
        """
        pcgs_old = self.pcgs_new
        pcgs_new = []
        pcgs_new_append = pcgs_new.append

        interface_constant_prob_old = self.interface_constant_prob_new
        interface_constant_prob_new = []
        interface_constant_prob_new_append = interface_constant_prob_new.append

        crystal_size_array_old = self.crystal_size_array_new
        crystal_size_array_new = []
        crystal_size_array_new_append = crystal_size_array_new.append

        pcg_chem_weath_array_old = self.pcg_chem_weath_array_new
        pcg_chem_weath_array_new = []
        pcg_chem_weath_array_new_append = pcg_chem_weath_array_new.append

        c_creator = np.random.RandomState(step)
        c = c_creator.random(size=self.pcg_additions[step-1] + 1)

        mcg_temp = [[[]
                    for m in range(self.n_minerals)]
                    for n in range(self.n_timesteps)]
    #         interface_indices = List()

        for i, (pcg, prob, csize, chem) in enumerate(
            zip(pcgs_old,
                interface_constant_prob_old,
                crystal_size_array_old,
                pcg_chem_weath_array_old)):

            pcg_length = pcg.size

            if self.enable_interface_location_prob:
                # Calculate interface location probability
                if pcg_length <= self.n_standard_cases:
                    location_prob = \
                        self.standard_prob_loc_cases[pcg_length - 1]
                else:
                    location_prob = create_interface_location_prob(pcg)

                # Calculate normalized probability
                probability_normalized = \
                    calculate_normalized_probability(location_prob, prob)

            else:
                probability_normalized = gen.normalize(prob)

            # Select interface to break pcg on
            interface = select_interface(i, probability_normalized, c)

            if self.enable_multi_pcg_breakage:
                prob_selected = probability_normalized[interface]
                print(prob_selected)
                interfaces_selected = \
                    np.where(probability_normalized > prob_selected)[0]
                print(interfaces_selected, interfaces_selected.size)

                pcg_new = np.array_split(pcg, interfaces_selected)
                csize_new = np.array_split(csize, interfaces_selected)
                chem_new = np.array_split(chem, interfaces_selected)
                prob_new = np.array_split(prob, interfaces_selected)

            else:
                # Using indexing instead of np.split is faster.
                # Also avoids the problem of possible 2D arrays instead of
                # 1D being created if array gets split in half.
                # Evuluate first new pcg
                if interface != 1:  # This implies that len(new_prob) != 0
                    pcgs_new_append(pcg[:interface])
                    crystal_size_array_new_append(csize[:interface])
                    pcg_chem_weath_array_new_append(chem[:interface])
                    interface_constant_prob_new_append(prob[:interface-1])
                else:
                    mcg_temp[chem[interface-1]][pcg[interface-1]]\
                        .append(csize[interface-1])

                # Evaluate second new pcg
                if pcg_length - interface != 1:
                    pcgs_new_append(pcg[interface:])
                    crystal_size_array_new_append(csize[interface:])
                    pcg_chem_weath_array_new_append(chem[interface:])
                    interface_constant_prob_new_append(prob[interface:])
                else:
                    mcg_temp[chem[interface]][pcg[interface]]\
                        .append(csize[interface])

                # Remove interface from interface_counts_matrix
                # Faster to work with matrix than with list and post-loop
                # operations as with the mcg counting
                self.interface_counts_matrix[pcg[interface-1], pcg[interface]] -= 1
                # interface_indices.append((pcg[interface-1], pcg[interface]))

        # Add counts from mcg_temp to mcg
        mcg_temp_matrix = np.zeros((self.n_timesteps,
                                    self.n_minerals,
                                    self.n_bins),
                                   dtype=np.uint32)
        for n, outer_list in enumerate(mcg_temp):
            for m, inner_list in enumerate(outer_list):
                # print(type(inner_list), len(inner_list))
                if inner_list:
                    mcg_temp_matrix[n, m] = \
                        np.bincount(inner_list,
                                    minlength=self.n_bins)
                    # sedgen.count_items(inner_list, self.n_bins)

        mcg_new = self.mcg.copy()
        mcg_new += mcg_temp_matrix

        return pcgs_new, crystal_size_array_new, interface_constant_prob_new, \
            pcg_chem_weath_array_new, mcg_new

    def mineral_property_setter(self, p):
        """Assigns a specified property to multiple mineral classes if
        needed"""

        # TODO:
        # Incorporate option to have a property specified per timestep.

        if len(p) == 1:
            return np.array([p] * self.n_minerals)
        elif len(p) == self.n_minerals:
            return np.array(p)
        else:
            raise ValueError("p should be of length 1 or same length as"
                             "minerals")

    def intra_crystal_breakage_binned(self, alternator, start_bin_corr=5):
        mcg_new = np.zeros_like(self.mcg)
        residue_new = \
            np.zeros((self.n_timesteps, self.n_minerals), dtype=np.float64)
        residue_count_new = \
            np.zeros((self.n_timesteps, self.n_minerals), dtype=np.uint32)

        for n in range(self.n_timesteps):
            for m, m_old in enumerate(self.mcg[n]):
                if all(m_old == 0):
                    mcg_new[n, m] = m_old
                else:
                    m_new, residue_add, residue_count_add = \
                        self.perform_intra_crystal_breakage_2d(
                            m_old,
                            m, n,
                            floor=alternator % 2,
                            intra_cb_threshold_bin=self.intra_cb_threshold_bin_matrix[n, m]+start_bin_corr,
                            start_bin_corr=start_bin_corr)
                    mcg_new[n, m] = m_new
                    residue_new[n, m] = residue_add
                    residue_count_new[n, m] = residue_count_add

        residue_new = np.sum(residue_new, axis=0)
        residue_count_new = np.sum(residue_count_new, axis=0)

        return mcg_new, residue_new, residue_count_new

    def perform_intra_crystal_breakage_2d(self, mcg_old, m, n,
                                          intra_cb_threshold_bin=200,
                                          floor=True, start_bin_corr=5):
        # Certain percentage of mcg has to be selected for intra_cb
        # Since mcg are already binned it doesn't matter which mcg get
        # selected in a certain bin, only how many

        prob = self.intra_cb_p
        search_bins = self.search_volume_bins_medians_matrix[m, n]
        intra_cb_breaks = self.intra_cb_breaks_matrix[m, n]
        diffs_volumes = self.diffs_volumes_matrix[n, m]

        mcg_new = mcg_old.copy()

        residue_count = 0
        residue_new = 0

        # 1. Select mcg
        if floor:
            # 1st time selection
            mcg_selected = \
                np.floor(mcg_new * prob[m]).astype(np.uint32)
        else:
            # 2nd time selection
            mcg_selected = \
                np.ceil(mcg_new * prob[m]).astype(np.uint32)

        # Sliced so that only the mcg above the intra_cb_threshold_bin are
        # affected; same reasoning in for loop below.
        mcg_new[intra_cb_threshold_bin:] -= mcg_selected[intra_cb_threshold_bin:]

        # 2. Create break points
        for i, n_crystals in enumerate(mcg_selected[intra_cb_threshold_bin:]):
            if n_crystals == 0:
                continue
            intra_cb_breaks_to_use = \
                intra_cb_breaks[i+start_bin_corr][diffs_volumes[i+start_bin_corr] > 0]
            diffs_volumes_to_use = \
                diffs_volumes[i+start_bin_corr][diffs_volumes[i+start_bin_corr] > 0]

            breaker_size, breaker_remainder = \
                divmod(n_crystals, intra_cb_breaks_to_use.size)

            breaker_counts = \
                np.array([breaker_size] * intra_cb_breaks_to_use.size,
                         dtype=np.uint32)
            breaker_counts[-1] += breaker_remainder

            p1 = i + intra_cb_threshold_bin \
                - np.arange(1, breaker_counts.size+1)
            p2 = p1 - intra_cb_breaks_to_use

            mcg_new[p1] += breaker_counts
            mcg_new[p2] += breaker_counts

            residue_new += \
                np.sum(search_bins[i + intra_cb_threshold_bin + self.n_bins] *
                       diffs_volumes_to_use * breaker_counts)

        return mcg_new, residue_new, residue_count

    def chemical_weathering_mcg(self, shift=1):
        # Reduce size/volume of selected mcg by decreasing their
        # size/volume bin array by one
        mcg_new = np.roll(self.mcg, shift=shift, axis=0)

        # Redisue
        # 1. Residue from mcg being in a negative grain size class
        residue_1 = np.zeros(self.n_minerals, dtype=np.float64)
        for n in range(1, self.n_timesteps):
            for m in range(self.n_minerals):
                threshold = self.negative_volume_thresholds[n, m]
                residue_1[m] += \
                    np.sum(mcg_new[n, m, :threshold] *
                           self.volume_bins_medians_matrix[n-1, m, :threshold])
                # Remove mcg from mcg array that have been added to
                # residue
                mcg_new[n, m, :threshold] = 0

        # 2. Residue from material being dissolved
        # By multiplying the volume change matrix with the already
        # 'rolled' mcg array and summing this over the mineral classes,
        # we end up with the total residue per mineral formed by
        # 'dissolution'.
        residue_2 = \
            np.sum(
                np.sum(mcg_new[1:] * self.volume_change_matrix, axis=0),
                axis=1)
        residue_per_mineral = residue_1 + residue_2

        # Make sure that mcg in last chemical weathering state are not
        # reintroduced to zero chemical weathering state
        if not (mcg_new[0] == 0).all():
            mcg_new[-1] += mcg_new[0]
            mcg_new[0] = 0

            warnings.warn("End of chemical states reached, mcg that were "
                          "reintroduced at zero chemical weathering state "
                          "during chem_weath_mcg have been rolled back to "
                          "last chemical weathering state.", UserWarning)

        return mcg_new, residue_per_mineral

    def chemical_weathering_pcg(self, shift=1):
        """Not taking into account that crystals on the inside of the
        pcg will be less, if even, affected by chemical weathering than
        those on the outside of the pcg"""

        residue_per_mineral = np.zeros(self.n_minerals, dtype=np.float64)

        pcg_lengths = np.array([len(pcg) for pcg in self.pcgs_new],
                               dtype=np.uint32)

        pcg_concat = np.concatenate(self.pcgs_new)
        csize_concat = np.concatenate(self.crystal_size_array_new)
        chem_concat_old = np.concatenate(self.pcg_chem_weath_array_new)

        chem_concat = chem_concat_old + 1

        thresholds_concat = \
            self.negative_volume_thresholds[chem_concat, pcg_concat]

        remaining_crystals = csize_concat >= thresholds_concat
        dissolved_crystals = np.where(csize_concat < thresholds_concat)

        pcg_remaining = pcg_concat[remaining_crystals]
        csize_remaining = csize_concat[remaining_crystals]
        chem_remaining = chem_concat[remaining_crystals]
        chem_old_remaining = chem_concat_old[remaining_crystals]

        pcg_filtered = \
            np.array_split(remaining_crystals, np.cumsum(pcg_lengths[:-1]))

        pcg_lengths_remaining = \
            np.array([len(pcg[pcg]) for pcg in pcg_filtered],
                     dtype=np.uint32)

        pcg_lengths_cumul = np.cumsum(pcg_lengths_remaining)

        zero_indices = np.where(pcg_lengths_remaining == 0)
        count_0 = zero_indices[0].size
        pcg_lengths_cumul_zero_deleted = np.delete(pcg_lengths_cumul,
                                                   zero_indices)

        pcg_remaining_list = \
            np.array_split(pcg_remaining, pcg_lengths_cumul[:-1])
        csize_remaining_list = \
            np.array_split(csize_remaining, pcg_lengths_cumul[:-1])
        chem_remaining_list = \
            np.array_split(chem_remaining, pcg_lengths_cumul[:-1])

        # Mcg accounting
        pcg_to_mcg = \
            pcg_remaining[pcg_lengths_cumul[pcg_lengths_remaining == 1] - 1]
        csize_to_mcg = \
            csize_remaining[pcg_lengths_cumul[pcg_lengths_remaining == 1] - 1]
        chem_to_mcg = \
            chem_remaining[pcg_lengths_cumul[pcg_lengths_remaining == 1] - 1]

        mcg_csize_unq, mcg_csize_ind, mcg_csize_cnt = \
            np.unique(csize_to_mcg, return_index=True, return_counts=True)

        self.mcg[chem_to_mcg[mcg_csize_ind],
                 pcg_to_mcg[mcg_csize_ind],
                 mcg_csize_unq] += mcg_csize_cnt.astype(np.uint32)

        # Interfaces counts
        pcg_concat_for_interfaces = \
            np.insert(pcg_remaining,
                      pcg_lengths_cumul[:-1].astype(np.int64),
                      self.n_minerals)

        interface_counts_matrix_new = \
            gen.count_and_convert_interfaces_to_matrix(
                pcg_concat_for_interfaces, self.n_minerals)

        # Interface probability calculations
        csize_concat_for_interfaces = \
            csize_remaining.copy().astype(np.int16)
        csize_concat_for_interfaces = \
            np.insert(csize_concat_for_interfaces,
                      pcg_lengths_cumul[:-1].astype(np.int64),
                      -1)

        interface_size_prob_concat = \
            gen.get_interface_size_prob(csize_concat_for_interfaces)
        interface_size_prob_concat = \
            interface_size_prob_concat[interface_size_prob_concat > 0]

        interface_strength_prob_concat = \
            gen.get_interface_strengths_prob(
                gen.expand_array(self.interface_proportions_normalized),
                pcg_concat_for_interfaces)
        interface_strength_prob_concat = \
            interface_strength_prob_concat[interface_strength_prob_concat > 0]

        prob_remaining = \
            interface_size_prob_concat / interface_strength_prob_concat

        prob_remaining_list = \
            np.array_split(
                prob_remaining, pcg_lengths_cumul_zero_deleted[:-1] -
                np.arange(1, len(pcg_remaining_list) - count_0))

        # Residue accounting
        pcg_dissolved = pcg_concat[dissolved_crystals]
        csize_dissolved = csize_concat[dissolved_crystals]
        chem_old_dissolved = chem_concat_old[dissolved_crystals]
        # 1. Residue from mcg being in a negative grain size class
        volumes_old_selected = \
            self.volume_bins_medians_matrix[chem_old_dissolved,
                                            pcg_dissolved,
                                            csize_dissolved]

        residue_1 = \
            gen.weighted_bin_count(pcg_dissolved,
                                   volumes_old_selected,
                                   self.n_minerals)

        # 2. Residue from material being weathered
        dissolved_volume_selected = \
            self.volume_change_matrix[chem_old_remaining,
                                      pcg_remaining,
                                      csize_remaining]
        residue_2 = \
            gen.weighted_bin_count(pcg_remaining,
                                   dissolved_volume_selected,
                                   self.n_minerals)

        # Add residue together per mineral
        residue_per_mineral = residue_1 + residue_2

        # Removing pcg that have been dissolved or have moved to mcg
        # print(pcg_remaining_list[:100])
        pcgs_new = \
            [pcg for pcg in pcg_remaining_list if pcg.size > 1]
        crystal_size_array_new = \
            [pcg for pcg in csize_remaining_list if pcg.size > 1]
        pcg_chem_weath_array_new = \
            [pcg for pcg in chem_remaining_list if pcg.size > 1]
        interface_constant_prob_new = \
            [prob for prob in prob_remaining_list if prob.size != 0]

        return pcgs_new, crystal_size_array_new, interface_constant_prob_new, \
            pcg_chem_weath_array_new, residue_per_mineral, \
            interface_counts_matrix_new

    def calculate_mass_balance_difference(self):
        return self.mass_balance[1:] - self.mass_balance[:-1]


# Deprecated?
def calculate_number_proportions_pcg(pcg_array):
    """Calculates the number proportions of the mineral classes present

    Parameters:
    -----------
    pcg_array : np.array
        Array holding the mineral identities of the crystals part of
        poly-crystalline grains

    Returns:
    --------
    number_proportions : np.array
        Normalized number proportions of crystals forming part of
        poly-crystalline grains
    """
    try:
        pcg_array = np.concatenate(pcg_array)
    except ValueError:
        pass
    crystals_count = np.bincount(pcg_array)
    print(crystals_count)
    number_proportions = gen.normalize(crystals_count)
    return number_proportions


# Deprecated?
def calculate_modal_mineralogy_pcg(pcg_array, csize_array, bins_volumes,
                                   return_volumes=True):
    """Calculates the volumetric proportions of the mineral classes
    present.

    Parameters:
    -----------
    pcg_array : np.array
        Array holding the mineral identities of the crystals part of
        poly-crystalline grains
    csize_array : np.array
        Array holding the crystal sizes in bin labels
    bins_volumes : np.array
        Bins to use for calculation of the crystal's volumes
    return_volumes : bool (optional)
        Whether to return the calculated volumes of the crystal or not;
        defaults to True

    Returns:
    --------
    modal_mineralogy: np.array
        Volumetric proportions of crystals forming part of
        poly-crystalline grains
    volumes : np.array
        Volumes of crystals forming part of poly-crystalline grains
    """
    try:
        pcg_array = np.concatenate(pcg_array)
        csize_array = np.concatenate(csize_array)
    except ValueError:
        pass

    volumes = bins_volumes[csize_array]
    volume_counts = gen.weighted_bin_count(pcg_array, volumes)
    modal_mineralogy = gen.normalize(volume_counts)

    if return_volumes:
        return modal_mineralogy, volumes
    else:
        return modal_mineralogy


@nb.njit(cache=True)
def create_transitions_correctly(row, c, N_initial):
    """https://stackoverflow.com/questions/40474436/how-to-apply-numpy-random-choice-to-a-matrix-of-probability-values-vectorized-s

    Check if freqs at end equals all zero to make sure function works.
    """

    # Absolute transition probabilities
    freqs = row.copy().astype(np.int32)

    # Number of transitions to obtain
    N = int(N_initial)

    # Normalize probabilities
    N_true = np.sum(freqs)

    # Initalize transition array
    transitions = np.zeros(shape=N, dtype=np.uint8)

    # For every transition
    for i in range(N):
        # Check were the random probability falls within the cumulative
        # probability distribution and select corresponding mineral
        choice = (c[i] < np.cumsum(freqs / N_true)).argmax()

        # Assign corresponding mineral to transition array
        transitions[i] = choice

        # Remove one count of the transition frequency array since we want to
        # have the exact number of absolute transitions based on the interface
        # proportions. Similar to a 'replace=False' in random sampling.
        freqs[choice] -= 1
        N_true -= 1

    return transitions


@nb.njit(cache=True)
def create_interface_array(minerals_N, transitions_per_mineral):
    # It's faster to work with list here and convert it to array
    # afterwards. 1m13s + 20s compared to 1m45s
    # True, but the list implementation uses around 9 GB memory while
    # the numpy one uses ca. 220 MB as per the array initialization.
    # The time loss of working with a numpy array from the start is
    # around 10 s compared to the list implementation.
    test_array = np.zeros(int(np.sum(minerals_N)), dtype=np.uint8)
    counters = np.array([0] * len(minerals_N), dtype=np.uint32)
    array_size_range = range(int(np.sum(minerals_N)))

    for i in array_size_range:
        previous_state = test_array[i-1]
        test_array[i] = \
            transitions_per_mineral[previous_state][counters[previous_state]]

        counters[previous_state] += 1

    return test_array

    # Numba provides a ca. 80x times speedup
    # 1.2s compared to 1m45s


def create_interface_location_prob(a):
    """Creates an array descending and then ascending again to represent
    chance of inter crystal breakage of a poly crystalline grain (pcg).
    The outer interfaces have a higher chance of breakage than the
    inner ones based on their location within the pcg.
    This represents a linear function.
    Perhaps other functions might be added (spherical) to see the
    effects later on.

    Not worth it adding numba to this function
    """
    size, corr = divmod(a.size, 2)
    ranger = np.arange(size, 0, -1, dtype=np.uint32)
    chance = np.append(ranger, ranger[-2+corr::-1])

    return chance


# Speedup from 6m30s to 2m45s
# Not parallelizable
@nb.njit(cache=True)
def select_interface(i, probs, c):
    interface = (c[i] < np.cumsum(probs)).argmax() + 1
    # The '+ 1' makes sure that the first interface can also be selected
    # Since the interface is used to slice the interface_array,
    # interface '0' would result in the pcg not to be broken at all
    # since e.g.:
    # np.array([0, 1, 2, 3, 4])[:0] = np.array([])

    return interface


# Speedup from 2m45s to 1m30s
# Parallelizable but not performant for small pcg
@nb.njit(cache=True)
def calculate_normalized_probability(location_prob, prob):
    probability = location_prob * prob
    return probability / np.sum(probability)