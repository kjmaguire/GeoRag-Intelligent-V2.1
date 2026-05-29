-- Phase G.1 (master-plan §8 + §20.2) — seed the 10 deposit-model templates.
--
-- Each row in `targeting.target_models` defines the deposit type's
-- geological grammar: typical host rocks, structures, alteration, pathfinder
-- geochemistry, geophysical signatures, plus positive / negative scoring
-- indicators. Each row also gets a row in `targeting.target_model_versions`
-- (version 1, weighted scoring) with per-factor weights summing to 1.0.
--
-- All weights are conservative v1 defaults chosen from canonical deposit-
-- model literature (Cox & Singer 1986 mineral-deposit models, Sillitoe
-- 2010 porphyry models, Jefferson 2007 Athabasca uranium synthesis,
-- Hitzman 1992 for IOCG/sedex). They will be tuned per workspace once
-- field outcomes feed back via `target_outcomes` + Phase 12 retraining.
--
-- Apply as superuser (laravel `georag_app` cannot INSERT into targeting.*
-- in single-tenant ESCAPE-HATCH mode without GUC plumbing). The seed is
-- ON CONFLICT DO NOTHING so it's safe to re-run.
--
-- Substrate verifier check `targeting:deposit-model-templates-seeded`
-- counts rows in target_models and fails when < 10.

SET search_path TO targeting, public;

-- ─────────────────────────── 1. Athabasca uranium ───────────────────────────
INSERT INTO targeting.target_models (
    slug, display_name, commodity_primary, commodities_secondary,
    attributes_payload, positive_indicators, negative_indicators,
    analogues_payload, recommended_next_data
) VALUES (
    'athabasca_uranium',
    'Athabasca Basin Unconformity-Related Uranium',
    'uranium',
    ARRAY['nickel','cobalt']::text[],
    jsonb_build_object(
        'host_rocks', jsonb_build_array('sandstone','conglomerate','basement_pelitic_gneiss','graphitic_metasediment'),
        'structures', jsonb_build_array('reactivated_basement_fault','graphite_conductor','unconformity_intersection'),
        'alteration', jsonb_build_array('clay_alteration_illite','chlorite','dravite_tourmaline','hematite','silicification'),
        'geochemistry', jsonb_build_object(
            'pathfinder_elements', jsonb_build_array('U','Ni','Co','As','Pb','B','REE'),
            'element_ratios', jsonb_build_array('Ni/Co','As/Cu','U/Th'),
            'anomaly_thresholds', jsonb_build_object('U_ppm', 100, 'Ni_ppm', 200, 'B_ppm', 500)
        ),
        'geophysics', jsonb_build_object(
            'magnetic_signature', 'magnetic_low_over_alteration_envelope',
            'radiometric_signature', 'discrete_uranium_anomaly',
            'gravity_signature', 'subtle_low_over_clay_alteration',
            'em_signature', 'strong_conductor_along_graphite',
            'ip_resistivity_signature', 'chargeable_within_clay_envelope'
        ),
        'tectonic_setting', jsonb_build_array('intracratonic_basin','paleoproterozoic_unconformity')
    ),
    jsonb_build_array(
        jsonb_build_object('factor','graphite_conductor_present','weight',0.25),
        jsonb_build_object('factor','clay_alteration_detected','weight',0.18),
        jsonb_build_object('factor','unconformity_within_500m','weight',0.20),
        jsonb_build_object('factor','U_geochem_anomaly','weight',0.20),
        jsonb_build_object('factor','structural_intersection','weight',0.12),
        jsonb_build_object('factor','historic_drill_intercept_within_2km','weight',0.05)
    ),
    jsonb_build_array(
        jsonb_build_object('factor','no_basement_pelite_in_section','weight',0.30),
        jsonb_build_object('factor','>2km_from_known_graphite','weight',0.25),
        jsonb_build_object('factor','penetrated_to_depth_no_alteration','weight',0.20)
    ),
    jsonb_build_array(
        jsonb_build_object('deposit','McArthur River','location','SK Canada','grade_pct_U3O8',17.0),
        jsonb_build_object('deposit','Cigar Lake','location','SK Canada','grade_pct_U3O8',18.0),
        jsonb_build_object('deposit','Triple R (Patterson Lake South)','location','SK Canada','grade_pct_U3O8',1.6)
    ),
    jsonb_build_array(
        'gravity_survey','dc_resistivity_ip','vtem_helitem_em','radon_track_etch','rcc_followup_drilling'
    )
) ON CONFLICT (slug) DO NOTHING;

-- ─────────────────────────── 2. Roll-front uranium ──────────────────────────
INSERT INTO targeting.target_models (
    slug, display_name, commodity_primary, commodities_secondary,
    attributes_payload, positive_indicators, negative_indicators,
    analogues_payload, recommended_next_data
) VALUES (
    'roll_front_uranium',
    'Sandstone-Hosted Roll-Front Uranium',
    'uranium',
    ARRAY[]::text[],
    jsonb_build_object(
        'host_rocks', jsonb_build_array('permeable_arkosic_sandstone','channel_sandstone','interbedded_mudstone'),
        'structures', jsonb_build_array('paleochannel_axis','redox_interface','permeability_pinch_out'),
        'alteration', jsonb_build_array('limonite','pyrite_reduced_zone','calcite_cement','organic_matter_concentration'),
        'geochemistry', jsonb_build_object(
            'pathfinder_elements', jsonb_build_array('U','Mo','Se','V','As'),
            'element_ratios', jsonb_build_array('Mo/U','Se/U'),
            'anomaly_thresholds', jsonb_build_object('U_ppm', 50, 'Mo_ppm', 10, 'Se_ppm', 1)
        ),
        'geophysics', jsonb_build_object(
            'magnetic_signature', 'flat_or_low_no_strong_response',
            'radiometric_signature', 'down-hole_gamma_anomaly_at_redox',
            'gravity_signature', 'flat',
            'em_signature', 'limited_value',
            'ip_resistivity_signature', 'chargeable_pyrite_zone_along_front'
        ),
        'tectonic_setting', jsonb_build_array('intracratonic_basin','passive_margin_sediments')
    ),
    jsonb_build_array(
        jsonb_build_object('factor','reduced_pyrite_zone_in_log','weight',0.25),
        jsonb_build_object('factor','redox_interface_within_section','weight',0.30),
        jsonb_build_object('factor','U_gamma_log_anomaly','weight',0.25),
        jsonb_build_object('factor','organic_matter_present','weight',0.10),
        jsonb_build_object('factor','permeable_sandstone_host','weight',0.10)
    ),
    jsonb_build_array(
        jsonb_build_object('factor','fully_oxidized_section','weight',0.40),
        jsonb_build_object('factor','impermeable_clay_dominant','weight',0.30)
    ),
    jsonb_build_array(
        jsonb_build_object('deposit','Smith Ranch-Highland','location','WY USA','grade_pct_U3O8',0.04),
        jsonb_build_object('deposit','Crow Butte','location','NE USA','grade_pct_U3O8',0.18),
        jsonb_build_object('deposit','Shirley Basin','location','WY USA','grade_pct_U3O8',0.09)
    ),
    jsonb_build_array(
        'down-hole_gamma_log','rotary_chip_logging','redox_state_eh_ph','permeability_test','isr_amenability'
    )
) ON CONFLICT (slug) DO NOTHING;

-- ─────────────────────────── 3. Orogenic gold ────────────────────────────────
INSERT INTO targeting.target_models (
    slug, display_name, commodity_primary, commodities_secondary,
    attributes_payload, positive_indicators, negative_indicators,
    analogues_payload, recommended_next_data
) VALUES (
    'orogenic_gold',
    'Orogenic (Mesothermal) Lode Gold',
    'gold',
    ARRAY['silver','arsenic','antimony']::text[],
    jsonb_build_object(
        'host_rocks', jsonb_build_array('greenschist_to_amphibolite_metavolcanic','metasedimentary_turbidite','banded_iron_formation','intrusion_proximal'),
        'structures', jsonb_build_array('second_third_order_shear_zone','dilational_jog','flexure_in_first_order_break'),
        'alteration', jsonb_build_array('sericite','carbonate_ankerite','silica_flooding','pyrite_arsenopyrite_albitization'),
        'geochemistry', jsonb_build_object(
            'pathfinder_elements', jsonb_build_array('Au','As','Sb','W','Te','Bi','Ag'),
            'element_ratios', jsonb_build_array('As/Au','Sb/Au'),
            'anomaly_thresholds', jsonb_build_object('Au_ppb', 100, 'As_ppm', 200, 'Sb_ppm', 50)
        ),
        'geophysics', jsonb_build_object(
            'magnetic_signature', 'linear_magnetic_break_along_shear',
            'radiometric_signature', 'none_diagnostic',
            'gravity_signature', 'flat_or_subtle',
            'em_signature', 'limited',
            'ip_resistivity_signature', 'chargeable_sulphide_along_structure'
        ),
        'tectonic_setting', jsonb_build_array('greenstone_belt','accretionary_orogen','craton_margin')
    ),
    jsonb_build_array(
        jsonb_build_object('factor','shear_zone_proximity_lt_200m','weight',0.30),
        jsonb_build_object('factor','quartz_carbonate_vein_observed','weight',0.20),
        jsonb_build_object('factor','As_Sb_geochem_anomaly','weight',0.20),
        jsonb_build_object('factor','sericite_carbonate_alteration','weight',0.15),
        jsonb_build_object('factor','Au_in_soil_or_till','weight',0.15)
    ),
    jsonb_build_array(
        jsonb_build_object('factor','no_structural_corridor','weight',0.35),
        jsonb_build_object('factor','unmetamorphosed_sedimentary_cover','weight',0.25)
    ),
    jsonb_build_array(
        jsonb_build_object('deposit','Detour Lake','location','ON Canada','grade_g_t_Au',0.95),
        jsonb_build_object('deposit','Macassa','location','ON Canada','grade_g_t_Au',22.0),
        jsonb_build_object('deposit','Kalgoorlie Golden Mile','location','WA Australia','grade_g_t_Au',2.4)
    ),
    jsonb_build_array(
        'detailed_structural_mapping','soil_geochem_grid','ip_resistivity','core_drilling_at_dilational_jog'
    )
) ON CONFLICT (slug) DO NOTHING;

-- ─────────────────────────── 4. Epithermal gold ─────────────────────────────
INSERT INTO targeting.target_models (
    slug, display_name, commodity_primary, commodities_secondary,
    attributes_payload, positive_indicators, negative_indicators,
    analogues_payload, recommended_next_data
) VALUES (
    'epithermal_gold',
    'Low- and High-Sulfidation Epithermal Gold',
    'gold',
    ARRAY['silver','copper']::text[],
    jsonb_build_object(
        'host_rocks', jsonb_build_array('rhyolite','dacite','andesite','volcaniclastic_lapilli_tuff'),
        'structures', jsonb_build_array('caldera_ring_fault','radial_fault','strike-slip_dilational_corridor'),
        'alteration', jsonb_build_array('advanced_argillic_alunite_kaolinite','silica_flooding_vuggy_silica','adularia_sericite','propylitic_halo'),
        'geochemistry', jsonb_build_object(
            'pathfinder_elements', jsonb_build_array('Au','Ag','As','Sb','Hg','Te','Mo'),
            'element_ratios', jsonb_build_array('Ag/Au','Hg/Au'),
            'anomaly_thresholds', jsonb_build_object('Au_ppb', 200, 'Ag_ppm', 5)
        ),
        'geophysics', jsonb_build_object(
            'magnetic_signature', 'magnetite_destructive_low',
            'radiometric_signature', 'potassium_high_at_adularia',
            'gravity_signature', 'flat',
            'em_signature', 'limited',
            'ip_resistivity_signature', 'silicified_resistivity_high'
        ),
        'tectonic_setting', jsonb_build_array('continental_arc','back_arc_extension','caldera_complex')
    ),
    jsonb_build_array(
        jsonb_build_object('factor','vuggy_silica_outcrop','weight',0.25),
        jsonb_build_object('factor','advanced_argillic_alteration','weight',0.20),
        jsonb_build_object('factor','Au_Ag_geochem_anomaly','weight',0.25),
        jsonb_build_object('factor','caldera_structure_proximity','weight',0.15),
        jsonb_build_object('factor','resistivity_high_in_silica','weight',0.15)
    ),
    jsonb_build_array(
        jsonb_build_object('factor','no_volcanic_cover','weight',0.40),
        jsonb_build_object('factor','deep_erosion_below_boiling_zone','weight',0.30)
    ),
    jsonb_build_array(
        jsonb_build_object('deposit','Yanacocha','location','Peru','grade_g_t_Au',1.0),
        jsonb_build_object('deposit','Hishikari','location','Japan','grade_g_t_Au',39.0),
        jsonb_build_object('deposit','Round Mountain','location','NV USA','grade_g_t_Au',0.5)
    ),
    jsonb_build_array(
        'shortwave_infrared_alteration_mapping','soil_geochem','ip_resistivity','rcc_to_boiling_zone'
    )
) ON CONFLICT (slug) DO NOTHING;

-- ─────────────────────────── 5. Porphyry copper ─────────────────────────────
INSERT INTO targeting.target_models (
    slug, display_name, commodity_primary, commodities_secondary,
    attributes_payload, positive_indicators, negative_indicators,
    analogues_payload, recommended_next_data
) VALUES (
    'porphyry_copper',
    'Porphyry Copper (Cu-Mo, Cu-Au)',
    'copper',
    ARRAY['molybdenum','gold','silver','rhenium']::text[],
    jsonb_build_object(
        'host_rocks', jsonb_build_array('granodiorite','quartz_monzonite','diorite_porphyry','andesite_volcanic_country_rock'),
        'structures', jsonb_build_array('lineament_intersection','radial_concentric_fault_array','breccia_pipe'),
        'alteration', jsonb_build_array('potassic_biotite_kspar_core','phyllic_quartz_sericite_pyrite','propylitic_chlorite_epidote','argillic_overprint'),
        'geochemistry', jsonb_build_object(
            'pathfinder_elements', jsonb_build_array('Cu','Mo','Au','Ag','Pb','Zn','Mn'),
            'element_ratios', jsonb_build_array('Cu/Mo','Au/Cu'),
            'anomaly_thresholds', jsonb_build_object('Cu_ppm', 300, 'Mo_ppm', 50)
        ),
        'geophysics', jsonb_build_object(
            'magnetic_signature', 'magnetite_core_high_with_pyrite_halo_low',
            'radiometric_signature', 'potassium_high_potassic_core',
            'gravity_signature', 'subtle_high_dense_sulphide_zone',
            'em_signature', 'limited',
            'ip_resistivity_signature', 'broad_chargeable_pyrite_halo_with_inner_resistive_silicified_core'
        ),
        'tectonic_setting', jsonb_build_array('continental_arc','island_arc','collisional_orogen')
    ),
    jsonb_build_array(
        jsonb_build_object('factor','potassic_core_alteration','weight',0.25),
        jsonb_build_object('factor','phyllic_halo_around_core','weight',0.20),
        jsonb_build_object('factor','Cu_Mo_geochem_anomaly','weight',0.25),
        jsonb_build_object('factor','ip_chargeability_halo','weight',0.15),
        jsonb_build_object('factor','intrusion_proximity_lt_500m','weight',0.15)
    ),
    jsonb_build_array(
        jsonb_build_object('factor','no_intrusion_intersected','weight',0.40),
        jsonb_build_object('factor','barren_pyrite_only_alteration','weight',0.25)
    ),
    jsonb_build_array(
        jsonb_build_object('deposit','Bingham Canyon','location','UT USA','grade_pct_Cu',0.65),
        jsonb_build_object('deposit','Chuquicamata','location','Chile','grade_pct_Cu',0.55),
        jsonb_build_object('deposit','Oyu Tolgoi','location','Mongolia','grade_pct_Cu',0.85)
    ),
    jsonb_build_array(
        'detailed_alteration_mapping','aster_shortwave_infrared','ip_resistivity_3d','diamond_drill_to_core_alteration'
    )
) ON CONFLICT (slug) DO NOTHING;

-- ─────────────────────────── 6. VMS ─────────────────────────────────────────
INSERT INTO targeting.target_models (
    slug, display_name, commodity_primary, commodities_secondary,
    attributes_payload, positive_indicators, negative_indicators,
    analogues_payload, recommended_next_data
) VALUES (
    'vms',
    'Volcanogenic Massive Sulfide (Cu-Zn-Pb-Au-Ag)',
    'copper',
    ARRAY['zinc','lead','gold','silver']::text[],
    jsonb_build_object(
        'host_rocks', jsonb_build_array('felsic_volcanic_dome_flow','volcaniclastic_rhyolite','mafic_basalt_pillow','exhalite_chert'),
        'structures', jsonb_build_array('synvolcanic_fault','feeder_breccia_zone','volcanic_dome_carapace'),
        'alteration', jsonb_build_array('sericite_chlorite_pipe','silicification','sulphide_stringer_zone','barite_chert_cap'),
        'geochemistry', jsonb_build_object(
            'pathfinder_elements', jsonb_build_array('Cu','Zn','Pb','Au','Ag','Ba','Sn','Tl'),
            'element_ratios', jsonb_build_array('Zn/(Zn+Pb)','Cu/(Cu+Zn)'),
            'anomaly_thresholds', jsonb_build_object('Cu_ppm', 500, 'Zn_ppm', 1000)
        ),
        'geophysics', jsonb_build_object(
            'magnetic_signature', 'pyrrhotite_magnetic_anomaly_if_present',
            'radiometric_signature', 'none_diagnostic',
            'gravity_signature', 'strong_high_dense_massive_sulphide',
            'em_signature', 'strong_conductor_massive_sulphide_lens',
            'ip_resistivity_signature', 'highly_chargeable_low_resistivity'
        ),
        'tectonic_setting', jsonb_build_array('mid_ocean_ridge','island_arc_back_arc','rifted_continental_margin')
    ),
    jsonb_build_array(
        jsonb_build_object('factor','sericite_chlorite_pipe_observed','weight',0.25),
        jsonb_build_object('factor','em_conductor_strong','weight',0.25),
        jsonb_build_object('factor','gravity_high_anomaly','weight',0.15),
        jsonb_build_object('factor','Zn_Cu_geochem_anomaly','weight',0.20),
        jsonb_build_object('factor','exhalite_chert_horizon_present','weight',0.15)
    ),
    jsonb_build_array(
        jsonb_build_object('factor','no_felsic_volcanic_unit','weight',0.40),
        jsonb_build_object('factor','no_em_response','weight',0.30)
    ),
    jsonb_build_array(
        jsonb_build_object('deposit','Kidd Creek','location','ON Canada','grade_pct_CuEq',5.6),
        jsonb_build_object('deposit','Rio Tinto','location','Spain','grade_pct_CuEq',3.0),
        jsonb_build_object('deposit','Neves-Corvo','location','Portugal','grade_pct_CuEq',5.2)
    ),
    jsonb_build_array(
        'fixed_loop_em','gravity_survey','litho-geochem_mapping','diamond_drill_through_alteration_pipe'
    )
) ON CONFLICT (slug) DO NOTHING;

-- ─────────────────────────── 7. SEDEX ───────────────────────────────────────
INSERT INTO targeting.target_models (
    slug, display_name, commodity_primary, commodities_secondary,
    attributes_payload, positive_indicators, negative_indicators,
    analogues_payload, recommended_next_data
) VALUES (
    'sedex',
    'Sedimentary Exhalative (Pb-Zn-Ag)',
    'zinc',
    ARRAY['lead','silver']::text[],
    jsonb_build_object(
        'host_rocks', jsonb_build_array('reduced_carbonaceous_shale','siltstone','dolomitic_carbonate','rift_basin_clastics'),
        'structures', jsonb_build_array('synsedimentary_growth_fault','rift_basin_margin','sub-basin_depocenter'),
        'alteration', jsonb_build_array('barite_zone','chert_lens','tourmaline','siderite'),
        'geochemistry', jsonb_build_object(
            'pathfinder_elements', jsonb_build_array('Zn','Pb','Ag','Ba','Tl','Cd'),
            'element_ratios', jsonb_build_array('Pb/(Pb+Zn)','Ba/Zn'),
            'anomaly_thresholds', jsonb_build_object('Zn_ppm', 1000, 'Pb_ppm', 500, 'Ba_pct', 1)
        ),
        'geophysics', jsonb_build_object(
            'magnetic_signature', 'subtle_low_or_flat',
            'radiometric_signature', 'none_diagnostic',
            'gravity_signature', 'high_dense_sulphide_layer',
            'em_signature', 'conductor_if_continuous_pyrite',
            'ip_resistivity_signature', 'chargeable_sulphide_horizon'
        ),
        'tectonic_setting', jsonb_build_array('intracratonic_rift','passive_margin_basin')
    ),
    jsonb_build_array(
        jsonb_build_object('factor','reduced_carbonaceous_shale_host','weight',0.25),
        jsonb_build_object('factor','growth_fault_proximity','weight',0.20),
        jsonb_build_object('factor','barite_zone_present','weight',0.20),
        jsonb_build_object('factor','Zn_Pb_geochem_anomaly','weight',0.20),
        jsonb_build_object('factor','gravity_high','weight',0.15)
    ),
    jsonb_build_array(
        jsonb_build_object('factor','oxidized_red_bed_section','weight',0.35),
        jsonb_build_object('factor','no_growth_fault_corridor','weight',0.25)
    ),
    jsonb_build_array(
        jsonb_build_object('deposit','Red Dog','location','AK USA','grade_pct_ZnEq',16.0),
        jsonb_build_object('deposit','Mount Isa George Fisher','location','QLD Australia','grade_pct_ZnEq',12.0),
        jsonb_build_object('deposit','Sullivan','location','BC Canada','grade_pct_ZnEq',11.7)
    ),
    jsonb_build_array(
        'stratigraphic_drilling','basin_modeling','core_litho-geochem','gravity_3d_inversion'
    )
) ON CONFLICT (slug) DO NOTHING;

-- ─────────────────────────── 8. Lithium pegmatite ───────────────────────────
INSERT INTO targeting.target_models (
    slug, display_name, commodity_primary, commodities_secondary,
    attributes_payload, positive_indicators, negative_indicators,
    analogues_payload, recommended_next_data
) VALUES (
    'lithium_pegmatite',
    'LCT (Lithium-Cesium-Tantalum) Pegmatite',
    'lithium',
    ARRAY['tantalum','cesium','tin','niobium']::text[],
    jsonb_build_object(
        'host_rocks', jsonb_build_array('pegmatite_dike','peraluminous_granite_parent','metasedimentary_country_rock_amphibolite_facies'),
        'structures', jsonb_build_array('dilational_fault_jog','dike_swarm','intrusion_carapace'),
        'alteration', jsonb_build_array('greisen_muscovite','tourmaline_aureole','potassic_aureole'),
        'geochemistry', jsonb_build_object(
            'pathfinder_elements', jsonb_build_array('Li','Cs','Ta','Nb','Sn','Be','Rb'),
            'element_ratios', jsonb_build_array('K/Rb','Nb/Ta'),
            'anomaly_thresholds', jsonb_build_object('Li2O_pct', 0.5, 'Ta2O5_ppm', 50, 'Cs_ppm', 30)
        ),
        'geophysics', jsonb_build_object(
            'magnetic_signature', 'magnetic_low_relative_to_country_rock',
            'radiometric_signature', 'potassium_high_in_K_feldspar',
            'gravity_signature', 'subtle_low_low_density_pegmatite',
            'em_signature', 'resistive',
            'ip_resistivity_signature', 'high_resistivity'
        ),
        'tectonic_setting', jsonb_build_array('post-collisional_orogen','archean_greenstone_belt_margin')
    ),
    jsonb_build_array(
        jsonb_build_object('factor','spodumene_visible_in_outcrop','weight',0.30),
        jsonb_build_object('factor','peraluminous_parent_intrusion_nearby','weight',0.20),
        jsonb_build_object('factor','Li_Cs_Ta_geochem_anomaly','weight',0.20),
        jsonb_build_object('factor','greisen_muscovite_alteration','weight',0.15),
        jsonb_build_object('factor','dike_orientation_favorable','weight',0.15)
    ),
    jsonb_build_array(
        jsonb_build_object('factor','no_pegmatite_outcrop_in_5km','weight',0.40),
        jsonb_build_object('factor','barren_simple_pegmatite_no_zoning','weight',0.30)
    ),
    jsonb_build_array(
        jsonb_build_object('deposit','Whabouchi','location','QC Canada','grade_pct_Li2O',1.5),
        jsonb_build_object('deposit','Greenbushes','location','WA Australia','grade_pct_Li2O',2.4),
        jsonb_build_object('deposit','Tanco','location','MB Canada','grade_pct_Li2O',2.8)
    ),
    jsonb_build_array(
        'mineralogical_mapping_xrd','hyperspectral_imagery','lidar_for_dike_geometry','spodumene_assay'
    )
) ON CONFLICT (slug) DO NOTHING;

-- ─────────────────────────── 9. Oil/gas basin ───────────────────────────────
INSERT INTO targeting.target_models (
    slug, display_name, commodity_primary, commodities_secondary,
    attributes_payload, positive_indicators, negative_indicators,
    analogues_payload, recommended_next_data
) VALUES (
    'oil_gas_basin',
    'Sedimentary Basin Hydrocarbon Play',
    'petroleum',
    ARRAY['natural_gas']::text[],
    jsonb_build_object(
        'host_rocks', jsonb_build_array('reservoir_sandstone','carbonate_reef','fractured_shale','source_rock_organic_shale'),
        'structures', jsonb_build_array('anticlinal_trap','fault_trap','stratigraphic_pinchout','reef_buildup'),
        'alteration', jsonb_build_array('source_rock_maturation_oil_window','vitrinite_reflectance_0.5_to_1.3','illitization'),
        'geochemistry', jsonb_build_object(
            'pathfinder_elements', jsonb_build_array(),
            'element_ratios', jsonb_build_array(),
            'anomaly_thresholds', jsonb_build_object('TOC_pct', 2, 'vitrinite_reflectance', 0.6)
        ),
        'geophysics', jsonb_build_object(
            'magnetic_signature', 'flat_sedimentary_basin',
            'radiometric_signature', 'organic_shale_uranium_anomaly',
            'gravity_signature', 'low_in_basin_centre',
            'em_signature', 'resistivity_contrast_at_hydrocarbon_water_contact',
            'ip_resistivity_signature', 'flat'
        ),
        'tectonic_setting', jsonb_build_array('foreland_basin','passive_margin','rift_basin','intracratonic_sag')
    ),
    jsonb_build_array(
        jsonb_build_object('factor','source_rock_within_oil_window','weight',0.30),
        jsonb_build_object('factor','reservoir_quality_porosity_perm','weight',0.25),
        jsonb_build_object('factor','trap_geometry_defined','weight',0.20),
        jsonb_build_object('factor','seal_continuous_shale_carbonate','weight',0.15),
        jsonb_build_object('factor','migration_pathway_intact','weight',0.10)
    ),
    jsonb_build_array(
        jsonb_build_object('factor','source_rock_overmature_gas_only','weight',0.20),
        jsonb_build_object('factor','no_trap_geometry','weight',0.35),
        jsonb_build_object('factor','breached_seal_natural_seeps','weight',0.25)
    ),
    jsonb_build_array(
        jsonb_build_object('deposit','Permian Basin','location','TX USA','play','Wolfcamp_Spraberry'),
        jsonb_build_object('deposit','Williston Basin','location','ND USA','play','Bakken'),
        jsonb_build_object('deposit','Western Canada Sedimentary Basin','location','AB Canada','play','Montney_Duvernay')
    ),
    jsonb_build_array(
        '3d_seismic_reprocessing','vitrinite_reflectance_pyrolysis','reservoir_petrophysics','well_test_pvt_sampling'
    )
) ON CONFLICT (slug) DO NOTHING;

-- ─────────────────────────── 10. Custom (workspace-defined) ────────────────
INSERT INTO targeting.target_models (
    slug, display_name, commodity_primary, commodities_secondary,
    attributes_payload, positive_indicators, negative_indicators,
    analogues_payload, recommended_next_data
) VALUES (
    'custom',
    'Custom (Workspace-Defined Deposit Model)',
    'unknown',
    ARRAY[]::text[],
    jsonb_build_object(
        '_note', 'Empty template — clone via target_models.create_workspace_variant() and populate per-workspace.',
        'host_rocks', jsonb_build_array(),
        'structures', jsonb_build_array(),
        'alteration', jsonb_build_array(),
        'geochemistry', jsonb_build_object('pathfinder_elements', jsonb_build_array(), 'element_ratios', jsonb_build_array(), 'anomaly_thresholds', jsonb_build_object()),
        'geophysics', jsonb_build_object('magnetic_signature', '', 'radiometric_signature', '', 'gravity_signature', '', 'em_signature', '', 'ip_resistivity_signature', ''),
        'tectonic_setting', jsonb_build_array()
    ),
    jsonb_build_array(),
    jsonb_build_array(),
    jsonb_build_array(),
    jsonb_build_array()
) ON CONFLICT (slug) DO NOTHING;


-- Active version 1 (weighted scoring) per model.
-- factor_weights are copied from positive_indicators[].weight so the
-- scoring engine has a flat lookup. Negative indicators carry their own
-- weights and are summed as score penalties; both halves are normalised
-- by the |sum_of_weights| at scoring time.
INSERT INTO targeting.target_model_versions (
    target_model_id, version, scoring_kind, factor_weights, is_active
)
SELECT
    target_model_id,
    1,
    'weighted',
    -- factor_weights: combine positive_indicators (with sign +1) and
    -- negative_indicators (with sign −1) into a flat name→weight map.
    -- COALESCE handles `custom`, which has empty arrays.
    COALESCE(
        (
            SELECT jsonb_object_agg(elem->>'factor', (elem->>'weight')::numeric)
            FROM (
                SELECT jsonb_array_elements(positive_indicators) AS elem
                UNION ALL
                SELECT jsonb_build_object('factor', elem->>'factor', 'weight', -1.0 * (elem->>'weight')::numeric) AS elem
                FROM jsonb_array_elements(negative_indicators) AS elem
            ) AS combined
        ),
        '{}'::jsonb
    ),
    true
FROM targeting.target_models
ON CONFLICT (target_model_id, version) DO NOTHING;
