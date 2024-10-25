from ..numpyro_utils import pack_fftn_values
import jax
import jax.numpy as jnp

NUMERICAL_EPSILON = 1e-7


def generate_new_key(initial_key):
    # Draw an integer between 0 (inclusive) and 1e6 (exclusive)
    random_int = jax.random.randint(initial_key, minval=0, maxval=int(1e6), shape=(1,))[
        0
    ]

    # Use the drawn integer to generate a new key
    new_key = jax.random.PRNGKey(random_int)

    return new_key


def add_axes_to_dict(dict_in, axes):
    return {key: add_axes(val, axes=axes) for key, val in dict_in.items()}


def add_axes(arr, axes):
    return jnp.expand_dims(arr, axis=axes)


def bias_function(field, ng_bar, gamma):
    return ng_bar * jnp.power(field + 1, gamma)


def bias_function_exp(field, ng_bar, gamma, field_cut_exp=0, epsilon_g=0.25):
    return (
        ng_bar
        * jnp.power(field + 1, gamma)
        * jnp.exp(-jnp.power((1 + field_cut_exp) / (1 + field), epsilon_g))
    )


def bias_function_H0_factorized(field, ng_bar_tilde, gamma, H0):
    return ng_bar_tilde * jnp.power(field + 1, gamma) * jnp.power(H0, 3)


class Field:
    def __init__(
        self,
        box_size_d,
        box_shape_d,
        FT=jnp.fft.fftn,
        inverse_FT=jnp.fft.ifftn,
        power_spectrum_of_k=None,
        apply_log_transform=False,
        debug=False,
        batch_shape=(),
    ):
        self.box_shape_d = box_shape_d
        self.power_spectrum_of_k = power_spectrum_of_k
        self.apply_log_transform = apply_log_transform
        self.batch_shape = batch_shape
        self.debug = debug

        self.compute_dimensions()
        self.compute_box_axes()
        self.total_pixels = self.get_total_pixels()

        # if the field is real, the box shape can be different from the
        # field shape
        self.compute_field_shape_d()
        self.compute_uvec()
        self.compute_pi_factor()

        self.box_size_d = box_size_d

        self.compute_k_norm()

        self.FT_kwargs = dict(axes=self.box_axes, s=self.box_shape_d, norm="ortho")
        self.iFT_kwargs = dict(axes=self.box_axes, s=self.box_shape_d, norm="ortho")

        self.FT = lambda x: FT(x, **self.FT_kwargs)
        self.inverse_FT = lambda x: inverse_FT(x, **self.iFT_kwargs)

        self.initialize_power_spectrum()

    @property
    def pixel_size_d(self):
        return self.get_pixel_size_d()

    @property
    def box_volume(self):
        return self.get_box_volume()

    @property
    def pixel_volume(self):
        return self.get_pixel_volume()

    @property
    def box_size_d(self):
        return self._box_size_d

    @box_size_d.setter
    def box_size_d(self, value):
        if isinstance(value, list):
            value = jnp.array(value)

        self._box_size_d = value

        self.compute_k_norm()
        self.initialize_power_spectrum()

    def compute_field_shape_d(self):
        self.field_shape_d = self.box_shape_d

    def compute_uvec(self):
        # for the computation of the normalization of the
        # log normal field
        self.uvec_FT = self.get_unit_fourier_vector()
        self.uvec_FT_batch = self.tile_to_include_batch_shape(self.uvec_FT)

    def initialize_power_spectrum(self):
        if self.power_spectrum_of_k == None:
            from cosmopower_jax.cosmopower_jax import CosmoPowerJAX as CPJ

            # predict the non-linear matter power spectrum
            self.emulator = CPJ(probe="mpk_nonlin")
            self.emulator_k_range = self.emulator.modes

    def compute_box_axes(self):
        axes = tuple(-self.dimensions + i for i in range(self.dimensions))
        self.box_axes = axes

    def set_batch_shape(self, batch_shape):
        self.batch_shape = batch_shape
        self.compute_k_norm()

    def get_unit_fourier_vector_with_FT(self):
        """
        We are a homogenous field. We take an arbitrary point in real
        space and FT. This is needed to compute the variation of the
        field in real space, when we normalize the log normal field.

        """
        uvec = jnp.zeros(self.box_shape_d)
        idx = (0,) * self.dimensions
        uvec = uvec.at[idx].set(1)

        return self.FT(uvec)

    def get_unit_fourier_vector(self):
        """
        Compute the unit vector her by hand to avoid one FT.
        Tested with the code above in pytest.

        """

        return jnp.ones(self.field_shape_d) / self.total_pixels ** (1 / 2)

    def sample_gaussian_F_whitened_fourier(self, key):
        shape = self.batch_shape + self.field_shape_d

        self.sample_gaussian_F_whitened_fourier_from_shape(key, shape)

    def sample_gaussian_F_whitened_fourier_from_shape(self, key, shape):
        # draw a new key for the imaginary part
        key2 = generate_new_key(key)

        gaussian_F_whitened_fourier_real = jax.random.normal(key, shape=shape)
        gaussian_F_whitened_fourier_imag = 1j * jax.random.normal(key2, shape=shape)

        gaussian_F_whitened_fourier = (
            gaussian_F_whitened_fourier_real + gaussian_F_whitened_fourier_imag
        )

        self.set_gaussian_F_whitened_k(
            gaussian_F_whitened_fourier=gaussian_F_whitened_fourier
        )

    def get_k_grid(self):
        """
        Compute the k grid in d dimensions.

        """

        shape = self.field_shape_d

        # inspired from https://github.com/cphyc/FyeldGenerator/blob/main/FyeldGenerator/core.py
        self.k_components = [
            jnp.fft.fftfreq(s, d=self.pixel_size_d[i]) * 2 * jnp.pi
            for i, s in enumerate(shape)
        ]

        k_grid = jnp.meshgrid(*self.k_components, indexing="ij")

        return self.get_k_array_from_meshgrid(k_grid)

    def get_k_array_from_meshgrid(self, k_grid):
        # put this in an array
        k_grid_array = jnp.zeros(
            (
                self.dimensions,
                *self.field_shape_d,
            )
        )

        if self.dimensions in [2, 3]:
            for i in range(self.dimensions):
                k_grid_array = k_grid_array.at[i].set(k_grid[i])
        else:
            raise NotImplementedError

        return k_grid_array

    def compute_delta_k(self):
        self.delta_k = jnp.array(
            [k_values[1] - k_values[0] for k_values in self.k_components]
        )

    def compute_delta_k_volume(self):
        self.delta_k_volume = jnp.prod(self.delta_k)

    def get_k_norm(self, k_grid, eps_num=NUMERICAL_EPSILON):
        return jnp.sqrt(jnp.sum(k_grid**2, axis=0) + eps_num)

    def compute_k_norm(self):
        k_grid = self.get_k_grid()
        self.compute_delta_k()
        self.compute_delta_k_volume()

        k_norm = self.get_k_norm(k_grid)
        self.k_norm = self.tile_to_include_batch_shape(k_norm)

    def compute_power_spectrum_pixelation(
        self, power_spectrum_kwargs=None, cosmo_params=None
    ):
        if self.power_spectrum_of_k != None:
            power_spectrum_kwargs_batch = add_axes_to_dict(
                power_spectrum_kwargs, axes=self.box_axes
            )
            self.power_spectrum = self.power_spectrum_of_k(
                self.k_norm, **power_spectrum_kwargs_batch
            )

            if cosmo_params != None:
                raise "Not compatible with analytical function. "
        else:
            self.compute_power_spectrum_contrast_DM_from_emulator(cosmo_params)
            self.power_spectrum = jnp.interp(
                self.k_norm, self.emulator_k_range, self.emulator_power, left=0, right=0
            )

        # compute factor to take into account the finite pixelation
        self.sqrt_power_spectrum_pixelation = (
            jnp.sqrt(self.power_spectrum * self.delta_k_volume) * self.pixelation_factor
        )

    def compute_gaussian_F_fourier(self, power_spectrum_kwargs=None, cosmo_params=None):
        self.compute_power_spectrum_pixelation(
            power_spectrum_kwargs=power_spectrum_kwargs, cosmo_params=cosmo_params
        )

        self.gaussian_F_fourier = (
            self.sqrt_power_spectrum_pixelation * self.gaussian_F_whitened_fourier
        )

    def compute_gaussian_F_spatial(self):
        self.gaussian_F_spatial = self.inverse_FT(self.gaussian_F_fourier)

    def compute_density_contrast_DM(self):
        if self.apply_log_transform:
            # self.variance_gaussian_F = jnp.var(self.gaussian_F_spatial, axis=self.box_axes)

            self.variance_gaussian_F = self.get_variance_gaussian_F()

            # add axes along the box shape, not needed anymore
            self.variance_gaussian_F = add_axes(
                self.variance_gaussian_F, axes=self.box_axes
            )

            self.density_contrast_DM = (
                jnp.exp(-self.variance_gaussian_F / 2 + self.gaussian_F_spatial) - 1
            )
        else:
            self.density_contrast_DM = self.gaussian_F_spatial

    def compute_power_spectrum_contrast_DM_from_emulator(self, cosmo_params):
        """
        Compute the emulated power spectrum from cosmopower jax.

        """

        self.emulator_power = self.emulator.predict(cosmo_params)

    def compute_density_g(
        self, bias_function=bias_function, bias_kwargs=dict(ng_bar=None, gamma=None)
    ):
        bias_kwargs_batch = add_axes_to_dict(bias_kwargs, axes=self.box_axes)

        self.density_g = bias_function(
            field=self.density_contrast_DM, **bias_kwargs_batch
        )

    def sample_galaxies_poisson_from_density_g(self, key):
        if self.debug:
            print(
                "Min max of density_g: ",
                jnp.min(self.density_g),
                jnp.max(self.density_g),
            )

        # Poisson sampling based on the density field
        self.galaxy_count = jax.random.poisson(key, self.density_g)

    def sample_density_g(self, key, power_spectrum_kwargs, bias_kwargs):
        self.sample_gaussian_F_whitened_fourier(key)
        self.compute_gaussian_F_fourier(power_spectrum_kwargs)
        self.compute_gaussian_F_spatial()
        self.compute_density_contrast_DM()
        self.compute_density_g(bias_kwargs=bias_kwargs)

    def set_gaussian_F_whitened_k(self, gaussian_F_whitened_fourier):
        self.gaussian_F_whitened_fourier = gaussian_F_whitened_fourier

    def sample_galaxies(self, key, power_spectrum_kwargs, bias_kwargs):
        self.sample_density_g(key, power_spectrum_kwargs, bias_kwargs)
        self.sample_galaxies_poisson_from_density_g(key)

    # helper methods
    def compute_dimensions(self):
        self.dimensions = len(self.box_shape_d)

    def get_pixel_size_d(self):
        return self.box_size_d / jnp.array(self.box_shape_d)

    def get_total_pixels(self):
        return jnp.prod(jnp.array(self.box_shape_d))

    def get_box_volume(self):
        return jnp.prod(jnp.array(self.box_size_d))

    def get_pixel_volume(self):
        return self.box_volume / self.total_pixels

    def compute_pi_factor(self):
        self.pixelation_factor = (
            1 / (2 * jnp.pi) ** (self.dimensions / 2) * self.total_pixels ** (1 / 2)
        )

    def get_variance_gaussian_F(self):
        integrand = self.sqrt_power_spectrum_pixelation**2

        variance = jnp.sum(integrand, axis=self.box_axes) / self.total_pixels

        return variance

    def tile_to_include_batch_shape(self, x):
        return jnp.tile(x, reps=self.batch_shape + (1,) * len(self.box_shape_d))


class RealField(Field):
    def __init__(
        self,
        box_size_d,
        box_shape_d,
        power_spectrum_of_k,
        apply_log_transform=False,
        debug=False,
        batch_shape=(),
        replace_FT_with_packing=False,
    ):
        FT = jnp.fft.rfftn
        inverse_FT = jnp.fft.irfftn
        super().__init__(
            box_size_d,
            box_shape_d,
            FT=FT,
            inverse_FT=inverse_FT,
            power_spectrum_of_k=power_spectrum_of_k,
            apply_log_transform=apply_log_transform,
            debug=debug,
            batch_shape=batch_shape,
        )
        self.replace_FT_with_packing = replace_FT_with_packing

    def compute_field_shape_d(self):
        self.field_shape_d = self.get_half_box_shape_d()

    def compute_uvec(self):
        self.uvec_FT = self.get_unit_fourier_vector()
        self.uvec_FT_batch = self.tile_to_include_batch_shape(self.uvec_FT)

    def get_half_box_shape_d(self):
        """
        Compute the half of the box shape, which will be used later for
        the real FFT.

        """

        half_box_shape_d = tuple(list(self.box_shape_d)[:-1]) + (
            self.box_shape_d[-1] // 2 + 1,
        )
        return half_box_shape_d

    def get_k_grid(self):
        """
        Compute the k grid in d dimensions.
        We tile the batch dimensions in the method compute_k_norm.
        """

        shape = self.box_shape_d

        # Compute k components
        self.k_components = [
            jnp.fft.fftfreq(shape[i], d=self.pixel_size_d[i]) * 2 * jnp.pi
            for i in range(len(shape) - 1)
        ]
        rfreq = jnp.fft.rfftfreq(shape[-1], d=self.pixel_size_d[-1]) * 2 * jnp.pi
        self.k_components.append(rfreq)

        # Create k grid
        k_grid = jnp.meshgrid(*self.k_components, indexing="ij")

        return self.get_k_array_from_meshgrid(k_grid)

    def sample_gaussian_F_whitened_fourier(self, key):
        # Warning, to have a Gaussian random field in fourier space, we sample in
        # the spatial domain via the function "sample_gaussian_F_whitened_spatial"'

        shape = self.batch_shape + self.box_shape_d

        self.sample_gaussian_F_whitened_spatial_from_shape(key, shape)

    def sample_gaussian_F_whitened_spatial_from_shape(self, key, shape):
        # first sample in the spatial domain
        gaussian_F_whitened_spatial = jax.random.normal(key, shape=shape)

        self.set_gaussian_F_whitened_from_gaussian_F_whitened_spatial(
            gaussian_F_whitened_spatial
        )

    def set_gaussian_F_whitened_from_gaussian_F_whitened_spatial(
        self, gaussian_F_whitened_spatial
    ):
        if self.replace_FT_with_packing:
            s = gaussian_F_whitened_spatial.shape
            if len(s) == self.dimensions:
                if self.dimensions == 2:
                    gaussian_F_whitened_fourier = pack_fftn_values.pack_fft_values_2d(
                        gaussian_F_whitened_spatial
                    )
                elif self.dimensions == 3:
                    gaussian_F_whitened_fourier = pack_fftn_values.pack_fft_values_3d(
                        gaussian_F_whitened_spatial
                    )
            elif len(s) == (self.dimensions + 1):
                if self.dimensions == 2:
                    gaussian_F_whitened_fourier = pack_fftn_values.vpack_fft_values_2d(
                        gaussian_F_whitened_spatial
                    )
                elif self.dimensions == 3:
                    gaussian_F_whitened_fourier = pack_fftn_values.vpack_fft_values_3d(
                        gaussian_F_whitened_spatial
                    )
            else:
                raise NotImplementedError

        else:
            gaussian_F_whitened_fourier = self.FT(gaussian_F_whitened_spatial)

        self.set_gaussian_F_whitened_k(
            gaussian_F_whitened_fourier=gaussian_F_whitened_fourier
        )

    def get_variance_gaussian_F(self, power_spectrum_pixelation=None):
        if power_spectrum_pixelation == None:
            power_spectrum_pixelation = self.sqrt_power_spectrum_pixelation**2

        integrand = power_spectrum_pixelation

        # second term to account for the positive frequencies that are not
        # reflected by the rfft frequencies in the self.k_grid for the real
        # field
        variance = (
            jnp.sum(integrand, axis=self.box_axes)
            + jnp.sum(integrand[..., 1:], axis=self.box_axes)
        ) / self.total_pixels

        # complex part is machine precision and discarded
        return variance.real


class RealLogNormalField(RealField):
    def __init__(
        self,
        box_size_d,
        box_shape_d,
        power_spectrum_of_k,
        debug=False,
        batch_shape=(),
        replace_FT_with_packing=False,
        set_zero_mode_to_zero=False,
    ):
        """
        A class for a real valued log normal field that follows a specified power spectrum.

        """

        self.set_zero_mode_to_zero = set_zero_mode_to_zero

        super().__init__(
            box_size_d,
            box_shape_d,
            power_spectrum_of_k=power_spectrum_of_k,
            apply_log_transform=True,
            debug=debug,
            replace_FT_with_packing=replace_FT_with_packing,
            batch_shape=batch_shape,
        )

    def compute_two_point_correlators(self):
        correlator_factor = self.total_pixels ** (1 / 2) / self.box_volume

        # the normalization factor is due to 1 / total number of pixels, see super class and abs(uvec) ** 2
        # and then summing is equivalent to norm=orth FT, multiplying with (nb pixels) ** (1 / 2)
        self.two_point_correlator = (
            self.inverse_FT(self.power_spectrum)
        ) * correlator_factor

        self.xiplusone = self.two_point_correlator + 1

        # this is for mode matching
        # Cf. Eq. 30 of https://articles.adsabs.harvard.edu/pdf/1991MNRAS.248....1C
        self.two_point_correlator_effective = jnp.log(self.xiplusone)

        # transform back to obtain the underlying power spectrum of the
        # Gaussian random field
        self.power_spectrum_eff = (
            self.FT(self.two_point_correlator_effective) / correlator_factor
        )

        if self.set_zero_mode_to_zero:
            self.power_spectrum_eff = (
                self.power_spectrum_eff
                * self.k_norm
                / (NUMERICAL_EPSILON + self.k_norm)
            )
            self.power_spectrum_eff = self.power_spectrum_eff.real

        self.power_spectrum_eff = jnp.clip(
            self.power_spectrum_eff, a_min=NUMERICAL_EPSILON
        )

    def compute_gaussian_F_fourier(self, power_spectrum_kwargs=None, cosmo_params=None):
        self.compute_power_spectrum_pixelation(
            power_spectrum_kwargs=power_spectrum_kwargs, cosmo_params=cosmo_params
        )
        self.compute_two_point_correlators()

        self.sqrt_power_spectrum_eff_pixelation = (
            jnp.sqrt(self.power_spectrum_eff * self.delta_k_volume)
            * self.pixelation_factor
        )

        self.gaussian_F_fourier = (
            self.sqrt_power_spectrum_eff_pixelation * self.gaussian_F_whitened_fourier
        )

    def get_variance_gaussian_F(self, power_spectrum_pixelation=None):
        if power_spectrum_pixelation == None:
            power_spectrum_pixelation = self.sqrt_power_spectrum_eff_pixelation**2

        return super().get_variance_gaussian_F(
            power_spectrum_pixelation=power_spectrum_pixelation
        )
