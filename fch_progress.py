#!/usr/bin/env python3
"""fch_progress: Valheim achievement progress from a character's .fch save (profile version 46).

Shows what is still missing for crafted items, weapons, cooked food, built pieces, causes of death (incl. tree deaths),
enemy/boss/mini-boss kills (any difficulty, Normal+, Hard), fishing and trophies.

The admin bot uses it for /valheim progress (a player uploads their .fch). It also runs on its own:
    python3 fch_progress.py [-f] [-o LIST] file.fch   (see -h)

File layout (little-endian). Strings are 7-bit-length-prefixed UTF-8. "dict" = int32 count + (string, float32)*count,
"list" = int32 count + string*count. There are no delimiters or magic markers; you find each section by parsing the
previous one.

    int32 payload_len | payload | int32 64 | SHA-512(payload)
    payload:
        int32 version (46), int32 205 (stat count), int32 10 (stat sets)
        10 STAT SETS back to back, indexed by the game's DifficultyRequirement enum:
            0 RawStats  1 Any  2 Hammer  3 Casual  4 VeryEasy  5 Easy  6 Default (Normal)  7 Hard  8 VeryHard  9 Hardcore
            each set:
                float32[205]          PlayerStatType values (deaths by cause, tree/mine counts, ...)
                dict known worlds     world name -> seconds played
                dict known world keys, dict known commands
                int32 5, then 5 enemy-kill dicts: total, unarmed, magic, ranged, melee ($enemy_* -> kills)
                dict items picked up, dict items CRAFTED, dict pickables, dict foods eaten, dict pieces built
        world/map data (mostly zeros)
        player blob: name, id, stats, GP, inventory (items stored as 32-bit hashes), then
            list known recipes, dict stations (name -> level), list known materials, list tutorials,
            list uniques, list TROPHIES, list biomes, ...

Which set an achievement reads (from the game's Achievement code): its difficulty requirement picks the set. "Any" is
set 1; "Default" (Normal) is set 6; "Hard" is set 7. Kills made on a harder difficulty also count in every easier set from
Casual up, so a set means "this difficulty or harder". Set 0 is raw and is never used by achievements.
"""
import struct
import sys

# ---------------------------------------------------------------------------
# What each achievement requires, rebuilt from the game's own rules (Valheim 1.0.16, Deep North included).
# The game fills these lists at start-up from its item database, so they are derived here the same way
# (Achievements.Initialize / ObjectDB in assembly_valheim.dll). Tokens match the keys in the save's stat tables.
# Regenerate after a game update.
# ---------------------------------------------------------------------------

# AllItemCraft: every recipe that needs a crafting station, minus the items the game excludes.
# Station-less hand recipes (stone axe, club, ...), cooked food and smelting do not count here.
CRAFTABLE = [
    '$item_arrow_bloodgold', '$item_arrow_bronze', '$item_arrow_carapace', '$item_arrow_charred', '$item_arrow_fire',
    '$item_arrow_flint', '$item_arrow_frost', '$item_arrow_iron', '$item_arrow_needle', '$item_arrow_obsidian',
    '$item_arrow_poison', '$item_arrow_silver', '$item_arrow_wood', '$item_atgeir_blackmetal', '$item_atgeir_bronze',
    '$item_atgeir_gold', '$item_atgeir_gold_bloodlightning', '$item_atgeir_gold_frostfire', '$item_atgeir_gold_uncooked',
    '$item_atgeir_himminafl', '$item_atgeir_iron', '$item_axe2h_gold', '$item_axe2h_gold_bloodlightning',
    '$item_axe2h_gold_frostfire', '$item_axe_berzerkr', '$item_axe_berzerkr_blood', '$item_axe_berzerkr_lightning',
    '$item_axe_berzerkr_nature', '$item_axe_blackmetal', '$item_axe_bronze', '$item_axe_early', '$item_axe_flint',
    '$item_axe_gold', '$item_axe_gold_bloodlightning', '$item_axe_gold_frostfire', '$item_axe_gold_uncooked',
    '$item_axe_iron', '$item_axe_jotunbane', '$item_bakedpoteitr_uncooked', '$item_barleywinebase', '$item_battleaxe',
    '$item_battleaxe_blackmetal', '$item_battleaxe_crystal', '$item_battleaxe_gold_uncooked',
    '$item_battleaxe_skullsplittur', '$item_bell', '$item_bilebomb', '$item_blacksoup', '$item_bloodpudding',
    '$item_boarjerky', '$item_bolt_blackmetal', '$item_bolt_bloodgold', '$item_bolt_bone', '$item_bolt_carapace',
    '$item_bolt_charred', '$item_bolt_iron', '$item_bomb_dynamite', '$item_bombblob_frost', '$item_bombblob_lava',
    '$item_bombblob_morkhalla', '$item_bombblob_poison', '$item_bombblob_poisonelite', '$item_bombblob_tar', '$item_bow',
    '$item_bow_ashlands', '$item_bow_ashlandsblood', '$item_bow_ashlandsroot', '$item_bow_ashlandsstorm',
    '$item_bow_draugrfang', '$item_bow_finewood', '$item_bow_gold', '$item_bow_gold_bloodlightning',
    '$item_bow_gold_frostfire', '$item_bow_gold_uncooked', '$item_bow_huntsman', '$item_bow_snipesnap',
    '$item_breaddough', '$item_bronze', '$item_bronzenails', '$item_cape_ash', '$item_cape_asksvin',
    '$item_cape_deepnorth', '$item_cape_deepnorth_mage', '$item_cape_deerhide', '$item_cape_feather', '$item_cape_linen',
    '$item_cape_lox', '$item_cape_trollhide', '$item_cape_wolf', '$item_carrotsoup', '$item_catapult_ammo',
    '$item_catapult_bloodgold_ammo', '$item_catapult_training_ammo', '$item_ceramicplate', '$item_chest_berserker',
    '$item_chest_berserker_undead', '$item_chest_bronze', '$item_chest_carapace', '$item_chest_fenris',
    '$item_chest_flametal', '$item_chest_heavy_deepnorth', '$item_chest_heavy_gold_uncooked', '$item_chest_iron',
    '$item_chest_leather', '$item_chest_lox', '$item_chest_mage', '$item_chest_mage_ashlands',
    '$item_chest_mage_deepnorth', '$item_chest_mage_gold_uncooked', '$item_chest_medium_ashlands',
    '$item_chest_medium_deepnorth', '$item_chest_medium_gold_uncooked', '$item_chest_pcuirass', '$item_chest_rags',
    '$item_chest_root', '$item_chest_trollleather', '$item_chest_wolf', '$item_crossbow_arbalest',
    '$item_crossbow_bloodlightning_gold', '$item_crossbow_frostfire_gold', '$item_crossbow_gold',
    '$item_crossbow_gold_uncooked', '$item_crossbow_ripper', '$item_crossbow_ripper_blood',
    '$item_crossbow_ripper_lightning', '$item_crossbow_ripper_nature', '$item_cultivator', '$item_deerstew',
    '$item_demister', '$item_dvergrkey', '$item_egg_cooked', '$item_eyescream', '$item_feastashlands',
    '$item_feastblackforest', '$item_feastdeepnorth', '$item_feastmeadows', '$item_feastmistlands',
    '$item_feastmountains', '$item_feastoceans', '$item_feastplains', '$item_feastswamps', '$item_fierysvinstew',
    '$item_fireworkrocket_blue', '$item_fireworkrocket_cyan', '$item_fireworkrocket_green',
    '$item_fireworkrocket_purple', '$item_fireworkrocket_red', '$item_fireworkrocket_yellow', '$item_fish_raw',
    '$item_fishandbreaduncooked', '$item_fishingbait_ashlands', '$item_fishingbait_cave', '$item_fishingbait_deepnorth',
    '$item_fishingbait_forest', '$item_fishingbait_mistlands', '$item_fishingbait_ocean', '$item_fishingbait_plains',
    '$item_fishingbait_swamp', '$item_fishsoup', '$item_fishwraps', '$item_fistweapon_bjorn',
    '$item_fistweapon_bjorn_undead', '$item_fistweapon_fenris', '$item_fistweapon_frostfire_gold',
    '$item_fistweapon_gold', '$item_fistweapon_gold_bloodlightning', '$item_fistweapon_gold_uncooked',
    '$item_frostorbs_uncooked', '$item_graplinghook', '$item_helmet_berserker', '$item_helmet_berserker_undead',
    '$item_helmet_bronze', '$item_helmet_carapace', '$item_helmet_celebration', '$item_helmet_crown_of_valheim',
    '$item_helmet_drake', '$item_helmet_fenris', '$item_helmet_fishinghat', '$item_helmet_flametal',
    '$item_helmet_heavy_deepnorth', '$item_helmet_heavy_gold_uncooked', '$item_helmet_iron', '$item_helmet_leather',
    '$item_helmet_lox', '$item_helmet_mage', '$item_helmet_mage_ashlands', '$item_helmet_mage_deepnorth',
    '$item_helmet_mage_gold_uncooked', '$item_helmet_medium_ashlands', '$item_helmet_medium_deepnorth',
    '$item_helmet_medium_gold_uncooked', '$item_helmet_padded', '$item_helmet_root', '$item_helmet_trollleather',
    '$item_hoe', '$item_honeyglazedchickenuncooked', '$item_ironnails', '$item_kalechips_uncooked',
    '$item_keys_gold_uncooked', '$item_knife_blackmetal', '$item_knife_butcher', '$item_knife_chitin',
    '$item_knife_copper', '$item_knife_flint', '$item_knife_gold', '$item_knife_gold_bloodlightning',
    '$item_knife_gold_frostfire', '$item_knife_gold_uncooked', '$item_knife_silver', '$item_knife_skollandhati',
    '$item_legs_berserker', '$item_legs_berserker_undead', '$item_legs_bronze', '$item_legs_carapace',
    '$item_legs_fenris', '$item_legs_flametal', '$item_legs_heavy_deepnorth', '$item_legs_heavy_gold_uncooked',
    '$item_legs_iron', '$item_legs_leather', '$item_legs_lox', '$item_legs_mage', '$item_legs_mage_ashlands',
    '$item_legs_mage_deepnorth', '$item_legs_mage_gold_uncooked', '$item_legs_medium_ashlands',
    '$item_legs_medium_deepnorth', '$item_legs_medium_gold_uncooked', '$item_legs_pgreaves', '$item_legs_rags',
    '$item_legs_root', '$item_legs_trollleather', '$item_legs_wolf', '$item_lingondricka', '$item_loxpie_uncooked',
    '$item_mace2h_gold_bloodlightning', '$item_mace2h_gold_frostfire', '$item_mace_bronze', '$item_mace_eldner',
    '$item_mace_eldner_blood', '$item_mace_eldner_lightning', '$item_mace_eldner_nature', '$item_mace_gold',
    '$item_mace_gold_bloodlightning', '$item_mace_gold_frostfire', '$item_mace_gold_uncooked', '$item_mace_iron',
    '$item_mace_needle', '$item_mace_silver', '$item_magicallystuffedmushroomuncooked', '$item_marinatedgreens',
    '$item_mashedmeat', '$item_meadbasebugrepellent', '$item_meadbasebzerker', '$item_meadbaseeitr',
    '$item_meadbaseeitr_lingering', '$item_meadbasefrostresist', '$item_meadbasehasty', '$item_meadbasehealth',
    '$item_meadbasehealth_lingering', '$item_meadbasehealth_major', '$item_meadbasehealth_medium',
    '$item_meadbaselightfoot', '$item_meadbasepoisonresist', '$item_meadbasestamina', '$item_meadbasestamina_lingering',
    '$item_meadbasestamina_medium', '$item_meadbasestrength', '$item_meadbaseswimmer', '$item_meadbasetamer',
    '$item_meadbasetasty', '$item_meatballsmashedpoteitr', '$item_meatplatteruncooked', '$item_mechanicalspring',
    '$item_mincemeatsauce', '$item_mistharesupremeuncooked', '$item_moosekebab', '$item_mushroomomelette',
    '$item_oatmeallingonberryjam', '$item_oatmilk', '$item_onionsoup', '$item_oozebomb', '$item_ovenpancake_uncooked',
    '$item_pancakes', '$item_pickaxe_antler', '$item_pickaxe_blackmetal', '$item_pickaxe_bronze', '$item_pickaxe_iron',
    '$item_piquantpie_uncooked', '$item_pulledbear', '$item_queensjam', '$item_roastedcrustpie_uncooked',
    '$item_saddleasksvin', '$item_saddlelox', '$item_saddlemoose', '$item_salad', '$item_sausages',
    '$item_scorchingmedley', '$item_scythe', '$item_sealsoup', '$item_seekeraspic', '$item_serpentstew',
    '$item_sharpeningstone', '$item_shield_banded', '$item_shield_blackmetal', '$item_shield_blackmetal_tower',
    '$item_shield_bonetower', '$item_shield_bronzebuckler', '$item_shield_buckler_gold_uncooked',
    '$item_shield_carapace', '$item_shield_carapacebuckler', '$item_shield_flametal', '$item_shield_flametal_tower',
    '$item_shield_gold', '$item_shield_gold_tower', '$item_shield_goldbuckler', '$item_shield_iron_tower',
    '$item_shield_ironbuckler', '$item_shield_roots', '$item_shield_round_gold_uncooked', '$item_shield_serpentscale',
    '$item_shield_silver', '$item_shield_tower_gold_uncooked', '$item_shield_wood', '$item_shield_woodtower',
    '$item_shieldcore', '$item_shocklatesmoothie', '$item_sizzlingberrybroth', '$item_sledge_demolisher',
    '$item_sledge_gold', '$item_sledge_gold_uncooked', '$item_sledge_iron', '$item_smokedfish',
    '$item_smokedmoosemeat', '$item_sparklingshroomshake', '$item_spear_ancientbark', '$item_spear_bronze',
    '$item_spear_carapace', '$item_spear_chitin', '$item_spear_flint', '$item_spear_gold',
    '$item_spear_gold_bloodlightning', '$item_spear_gold_frostfire', '$item_spear_gold_uncooked',
    '$item_spear_splitner', '$item_spear_splitner_blood', '$item_spear_splitner_lightning',
    '$item_spear_splitner_nature', '$item_spear_wolffang', '$item_spicymarmalade', '$item_staff_frostorbs',
    '$item_staff_lightning', '$item_staff_orbofahri', '$item_staff_orbofahri_uncooked', '$item_staff_spiritcaller',
    '$item_staff_spiritcaller_uncooked', '$item_staff_thunderblood', '$item_staff_thunderblood_uncooked',
    '$item_staffclusterbomb', '$item_stafffireball', '$item_staffgreenroots', '$item_stafficeshards',
    '$item_staffredtroll', '$item_staffshield', '$item_staffskeleton', '$item_stagbreaker', '$item_sword2h_gold',
    '$item_sword2h_gold_bloodlightning', '$item_sword2h_gold_frostfire', '$item_sword2h_gold_uncooked',
    '$item_sword_blackmetal', '$item_sword_bronze', '$item_sword_dyrnwyn', '$item_sword_gold',
    '$item_sword_gold_bloodlightning', '$item_sword_gold_frostfire', '$item_sword_gold_uncooked', '$item_sword_iron',
    '$item_sword_krom', '$item_sword_mistwalker', '$item_sword_niedhogg', '$item_sword_niedhogg_blood',
    '$item_sword_niedhogg_lightning', '$item_sword_niedhogg_nature', '$item_sword_silver', '$item_sword_slayer',
    '$item_sword_slayer_blood', '$item_sword_slayer_lightning', '$item_sword_slayer_nature', '$item_sword_wood',
    '$item_trinketblackdamagedealth', '$item_trinketblackdtamina', '$item_trinketbloodgoldhealth',
    '$item_trinketbloodgoldstamina', '$item_trinketbronzehealth', '$item_trinketbronzestamina',
    '$item_trinketcarapaceeitr', '$item_trinketchitinswim', '$item_trinketflametaleitr',
    '$item_trinketflametalstaminahealth', '$item_trinketironhealth', '$item_trinketironstamina',
    '$item_trinketscalestaminadamage', '$item_trinketsilverdamage', '$item_trinketsilverresist', '$item_turnipstew',
    '$item_turretbolt', '$item_turretbolt_bloodgold', '$item_turretbolt_flametal', '$item_turretboltwood',
    '$item_vikingcupcake_uncooked', '$item_wolf_skewer', '$item_wolfjerky', '$item_yggdrasilporridge',
]

# AllBuildPieces: every piece in a build table that allows removing pieces (Hammer), except the repair tool.
# Cultivator and Hoe pieces do not count. Hidden/seasonal pieces do.
BUILDABLE = [
    '$item_hardrock', '$piece_BlackwoodStakewall', '$piece_archerytarget', '$piece_armorstand', '$piece_artisan_ext1',
    '$piece_artisanstation', '$piece_ashwood_archedwall', '$piece_ashwood_beam_1m', '$piece_ashwood_beam_2m',
    '$piece_ashwood_bed', '$piece_ashwood_decowall', '$piece_ashwood_decowall_divider', '$piece_ashwood_decowall_tree',
    '$piece_ashwood_door', '$piece_ashwood_floor_1x1', '$piece_ashwood_floor_2x2', '$piece_ashwood_floor_deco',
    '$piece_ashwood_halfwall', '$piece_ashwood_pole_1m', '$piece_ashwood_pole_2m', '$piece_ashwood_quarterwall',
    '$piece_ashwood_wall', '$piece_ashwoodarch_big', '$piece_ashwoodbeam26', '$piece_ashwoodbeam45',
    '$piece_ashwoodbeam67', '$piece_ashwoodcross26', '$piece_ashwoodcross45', '$piece_ashwoodstair',
    '$piece_ashwoodwallroof67', '$piece_ashwoodwallroof67upsidedown', '$piece_ashwoodwallroof_26',
    '$piece_ashwoodwallroof_26_upsidedown', '$piece_ashwoodwallroof_45', '$piece_ashwoodwallroof_45_upsidedown',
    '$piece_ashwoodwallrooftop67', '$piece_asksvinskeleton', '$piece_banner01', '$piece_banner02', '$piece_banner03',
    '$piece_banner04', '$piece_banner05', '$piece_banner06', '$piece_banner07', '$piece_banner08', '$piece_banner09',
    '$piece_banner10', '$piece_banner11', '$piece_barber', '$piece_bathtub', '$piece_bed', '$piece_bed02',
    '$piece_beehive', '$piece_bench01', '$piece_bench_runed', '$piece_benchlog', '$piece_birdnest',
    '$piece_blackforge', '$piece_blackforge_ext1', '$piece_blackforge_ext2', '$piece_blackforge_ext3',
    '$piece_blackforge_ext4', '$piece_blackforge_ext5', '$piece_blackmarble1x1', '$piece_blackmarble2x1x1',
    '$piece_blackmarble2x2x2', '$piece_blackmarble_arch', '$piece_blackmarble_base1', '$piece_blackmarble_basecorner',
    '$piece_blackmarble_bench', '$piece_blackmarble_column_1', '$piece_blackmarble_column_2',
    '$piece_blackmarble_floor', '$piece_blackmarble_floor_triangle', '$piece_blackmarble_out1',
    '$piece_blackmarble_outcorner', '$piece_blackmarble_stair', '$piece_blackmarble_table',
    '$piece_blackmarble_throne', '$piece_blackmarble_tip', '$piece_blackmetalbarstack', '$piece_blackwoodbench01',
    '$piece_blackwoodstack', '$piece_blastfurnace', '$piece_bloodgoldbarstack', '$piece_bone_throne',
    '$piece_bonestack', '$piece_bonfire', '$piece_brazierceiling01', '$piece_brazierfloor01', '$piece_brazierfloor02',
    '$piece_bronzebarstack', '$piece_candle', '$piece_cartographytable', '$piece_cauldron',
    '$piece_cauldron_ext1_spice', '$piece_cauldron_ext3_butchertable', '$piece_cauldron_ext4_pans',
    '$piece_cauldron_ext5_mortarandpestle', '$piece_cauldron_ext6_rollingpins', '$piece_cauldron_ext7_smoker',
    '$piece_celebrationgarland', '$piece_chair', '$piece_chair_runed', '$piece_charcoalkiln', '$piece_chest',
    '$piece_chestbarrel', '$piece_chestblackmetal', '$piece_chestgrausten', '$piece_chestprivate',
    '$piece_chesttreasure', '$piece_chestwarderobe', '$piece_chestwood', '$piece_clothdoor', '$piece_coalpile',
    '$piece_cookingstation', '$piece_cookingstation_iron', '$piece_copperbarstack', '$piece_crystalwall1x1',
    '$piece_darkwoodarch', '$piece_darkwoodbeam', '$piece_darkwoodbeam4', '$piece_darkwoodbeam67',
    '$piece_darkwoodbeam_26', '$piece_darkwoodbeam_45', '$piece_darkwoodchair', '$piece_darkwooddecowall',
    '$piece_darkwoodgate', '$piece_darkwoodpole', '$piece_darkwoodpole4', '$piece_darkwoodraven',
    '$piece_darkwoodroof26', '$piece_darkwoodroof45', '$piece_darkwoodroof67', '$piece_darkwoodrooficorner',
    '$piece_darkwoodrooficorner45', '$piece_darkwoodrooficorner67', '$piece_darkwoodroofocorner',
    '$piece_darkwoodroofocorner45', '$piece_darkwoodroofocorner67', '$piece_darkwoodrooftop',
    '$piece_darkwoodrooftop45', '$piece_darkwoodrooftop67', '$piece_darkwoodwolf', '$piece_deco_stave_wall',
    '$piece_drawbridge_dn', '$piece_drawbridge_log', '$piece_dvergr_lantern', '$piece_dvergr_lantern_pole',
    '$piece_dvergr_metal_wall', '$piece_dvergr_sharpstakes', '$piece_dvergr_spiralstair',
    '$piece_dvergr_spiralstair_right', '$piece_dvergr_stake_wall', '$piece_eitrrefinery', '$piece_faderember',
    '$piece_fairylightgarland', '$piece_fermenter', '$piece_firepit', '$piece_firepit_iron', '$piece_flametal_beam',
    '$piece_flametal_pillar', '$piece_flametalbarstack', '$piece_flametalgate', '$piece_flintpile', '$piece_forge',
    '$piece_forge_ext1', '$piece_forge_ext2', '$piece_forge_ext3', '$piece_forge_ext4', '$piece_forge_ext5',
    '$piece_forge_ext6', '$piece_frostfoundry', '$piece_frostkiln', '$piece_grausten_archmedium',
    '$piece_grausten_archsmall', '$piece_grausten_beammedium', '$piece_grausten_beamsmall',
    '$piece_grausten_floor1x1', '$piece_grausten_floor2x2', '$piece_grausten_floor4x4',
    '$piece_grausten_pillarmedium', '$piece_grausten_pillarsmall', '$piece_grausten_pillartapered',
    '$piece_grausten_pillartaperedinverted', '$piece_grausten_roof45', '$piece_grausten_roof45_arch',
    '$piece_grausten_roof45_archcorner', '$piece_grausten_roof45_archcorner2', '$piece_grausten_roof45_corner',
    '$piece_grausten_roof45_corner2', '$piece_grausten_stair', '$piece_grausten_stoneladder',
    '$piece_grausten_wall1x2', '$piece_grausten_wall2x2', '$piece_grausten_wall4x2', '$piece_grausten_wallarch',
    '$piece_grausten_wallarchinv', '$piece_grausten_window2x2', '$piece_grausten_window4x2', '$piece_graustenpile',
    '$piece_groundtorch', '$piece_groundtorchblue', '$piece_groundtorchdemister', '$piece_groundtorchgreen',
    '$piece_groundtorchwood', '$piece_guardstone', '$piece_hanging_cloth_blue1', '$piece_hanging_cloth_blue2',
    '$piece_hearth', '$piece_hexagonalgate', '$piece_hoodedlantern', '$piece_icecube', '$piece_incinerator',
    '$piece_ironbarstack', '$piece_ironfloor', '$piece_ironfloorSmall', '$piece_irongate', '$piece_ironwall',
    '$piece_ironwallSmall', '$piece_ironwoodbeam67', '$piece_itemstand', '$piece_jackoturnip', '$piece_jute_carpet',
    '$piece_juteblue_carpet', '$piece_lavalantern', '$piece_logbeam2', '$piece_logbeam4', '$piece_logpole2',
    '$piece_logpole4', '$piece_magetable', '$piece_magetable_ext', '$piece_magetable_ext2', '$piece_magetable_ext3',
    '$piece_magetable_ext4', '$piece_marblepile', '$piece_maypole', '$piece_meadcauldron', '$piece_mistletoe',
    '$piece_moose_throne', '$piece_oven', '$piece_portal', '$piece_portal_stone', '$piece_pot_large_green',
    '$piece_pot_medium_green', '$piece_pot_small_green', '$piece_preptable', '$piece_rug_asksvin', '$piece_rug_bjorn',
    '$piece_rug_deer', '$piece_rug_hare', '$piece_rug_lox', '$piece_rug_moose', '$piece_rug_seal', '$piece_rug_straw',
    '$piece_rug_wolf', '$piece_sapcollector', '$piece_scale_26', '$piece_scale_26_flipped',
    '$piece_scale_26_upsidedown', '$piece_scale_26_upsidedown_flipped', '$piece_scale_45', '$piece_scale_45_flipped',
    '$piece_scale_45_upsidedown', '$piece_scale_45_upsidedown_flipped', '$piece_scale_67', '$piece_scale_67_flipped',
    '$piece_scale_67_upsidedown', '$piece_scale_67_upsidedown_flipped', '$piece_scale_halfwall',
    '$piece_scale_quarterwall', '$piece_scale_wall', '$piece_sconce', '$piece_sharpstakes', '$piece_shieldgenerator',
    '$piece_sign', '$piece_silverbarstack', '$piece_skullpile', '$piece_smelter', '$piece_snowlantern',
    '$piece_spinningwheel', '$piece_stakewall', '$piece_stave_deco_beam_2m', '$piece_stave_deco_pole_2m',
    '$piece_stave_pole_2m', '$piece_stave_pole_4m', '$piece_stave_wall', '$piece_stavebeam2', '$piece_stavebeam26',
    '$piece_stavebeam4', '$piece_stavebeam45', '$piece_stavebeam67', '$piece_stavecross26', '$piece_stavecross45',
    '$piece_stavedecobeam26', '$piece_stavedecobeam45', '$piece_stavedecobeam67', '$piece_stavegate',
    '$piece_stavewallrooftop67', '$piece_stonearch', '$piece_stonecutter', '$piece_stonefence',
    '$piece_stonefloor2x2', '$piece_stonepile', '$piece_stonepillar', '$piece_stonestair', '$piece_stonethrone',
    '$piece_stonewall1x1', '$piece_stonewall2x1', '$piece_stonewall4x2', '$piece_stool', '$piece_table',
    '$piece_table_oak', '$piece_table_round', '$piece_table_runed', '$piece_table_runed_small', '$piece_throne01',
    '$piece_tinbarstack', '$piece_trainingdummy', '$piece_trap', '$piece_treasure_pile', '$piece_treasure_stack',
    '$piece_turret', '$piece_windmill', '$piece_wisplure', '$piece_woodbeam1', '$piece_woodbeam2',
    '$piece_woodbeam26', '$piece_woodbeam45', '$piece_woodbeam67', '$piece_woodcorestack', '$piece_wooddoor',
    '$piece_wooddragon', '$piece_woodfence', '$piece_woodfencegate', '$piece_woodfinestack', '$piece_woodfloor1x1',
    '$piece_woodfloor2x2', '$piece_woodfroststack', '$piece_woodgate', '$piece_woodironbeam', '$piece_woodironbeam_26',
    '$piece_woodironbeam_45', '$piece_woodironpole', '$piece_woodlog26', '$piece_woodlog45', '$piece_woodlog67',
    '$piece_woodpole', '$piece_woodpole2', '$piece_woodroof26', '$piece_woodroof45', '$piece_woodroof67',
    '$piece_woodrooficorner', '$piece_woodrooficorner45', '$piece_woodrooficorner67', '$piece_woodroofocorner',
    '$piece_woodroofocorner45', '$piece_woodroofocorner67', '$piece_woodrooftop', '$piece_woodrooftop45',
    '$piece_woodrooftop67', '$piece_woodstack', '$piece_woodstair', '$piece_woodstepladder', '$piece_woodwall',
    '$piece_woodwallhalf', '$piece_woodwallquarter', '$piece_woodwallroof', '$piece_woodwallroof45',
    '$piece_woodwallroof45_upsidedown', '$piece_woodwallroof67', '$piece_woodwallroof67upsidedown',
    '$piece_woodwallroof_upsidedown', '$piece_woodwallrooftop', '$piece_woodwallrooftop45',
    '$piece_woodwallrooftop67', '$piece_woodwindowshutter', '$piece_workbench', '$piece_workbench_ext1',
    '$piece_workbench_ext2', '$piece_workbench_ext3', '$piece_workbench_ext4', '$piece_yggdrasilstack',
    '$piece_yulecrown', '$piece_yulegarland', '$piece_yuleklapp', '$piece_yuletree', '$ship_karve', '$ship_longship',
    '$ship_longship_ashlands', '$ship_raft', '$tool_batteringram', '$tool_cart', '$tool_catapult',
]

# KillAllCreatures / KillAllCreaturesHard: every character the game flags as "killed for achievements",
# bosses split out into BOSSES below (the game lists them together, 108 in total).
ENEMIES = [
    '$enemy_abomination', '$enemy_asksvin', '$enemy_asksvin_hatchling', '$enemy_aspect_bonemass',
    '$enemy_aspect_dragon', '$enemy_aspect_eikthyr', '$enemy_aspect_fader', '$enemy_aspect_gdking',
    '$enemy_aspect_goblinking', '$enemy_aspect_seekerqueen', '$enemy_babyseeker', '$enemy_barka', '$enemy_bat',
    '$enemy_bjorn', '$enemy_blob', '$enemy_blobelite', '$enemy_blobfrost', '$enemy_bloblava', '$enemy_blobmork',
    '$enemy_blobmorkmini', '$enemy_blobtar', '$enemy_boar', '$enemy_boarpiggy', '$enemy_bonemawserpent',
    '$enemy_charred_archer', '$enemy_charred_mage', '$enemy_charred_melee', '$enemy_charred_melee_Dyrnwyn',
    '$enemy_charred_melee_Fader', '$enemy_charred_twitcher', '$enemy_charred_twitcher_summoned', '$enemy_chicken',
    '$enemy_deathsquito', '$enemy_deer', '$enemy_drake', '$enemy_draugr', '$enemy_draugrelite', '$enemy_dvergr',
    '$enemy_dvergr_deepnorth', '$enemy_dvergr_mage', '$enemy_elaking', '$enemy_elakingmole', '$enemy_fallenvalkyrie',
    '$enemy_fallenwarrior', '$enemy_fenring', '$enemy_fenringcultist', '$enemy_fenringcultist_hildir',
    '$enemy_frozenking', '$enemy_ghost', '$enemy_gjall', '$enemy_goblin', '$enemy_goblin_deepnorth',
    '$enemy_goblin_hildir', '$enemy_goblinbrute', '$enemy_goblinbrute_hildircombined', '$enemy_goblinshaman',
    '$enemy_greydwarf', '$enemy_greydwarfbrute', '$enemy_greydwarfshaman', '$enemy_greyling', '$enemy_hare',
    '$enemy_hen', '$enemy_jotun_warrior', '$enemy_jotun_witch', '$enemy_kvastur', '$enemy_leech', '$enemy_lox',
    '$enemy_loxcalf', '$enemy_moose', '$enemy_moosecalf', '$enemy_morgen', '$enemy_neck', '$enemy_root',
    '$enemy_seal', '$enemy_seal_baby', '$enemy_seeker', '$enemy_seekerbrute', '$enemy_serpent', '$enemy_skeleton',
    '$enemy_skeleton_summoned', '$enemy_skeletonfire', '$enemy_skeletonpoison', '$enemy_stonegolem',
    '$enemy_summonedtroll', '$enemy_surtling', '$enemy_tick', '$enemy_troll', '$enemy_trollfrost', '$enemy_ulv',
    '$enemy_unbjorn', '$enemy_volture', '$enemy_wolf', '$enemy_wolfcub', '$enemy_wraith', '$enemy_writhan',
    '$piece_trainingdummy', '$spiritcaller_bjorn', '$spiritcaller_boar', '$spiritcaller_moose', '$spiritcaller_wolf',
]

# AllBosses / AllBossesNormal / AllBossesHard (identical list; Frozen King counts via its phase 3 form)
BOSSES = [
    '$enemy_eikthyr', '$enemy_gdking', '$enemy_bonemass', '$enemy_dragon', '$enemy_goblinking', '$enemy_seekerqueen',
    '$enemy_fader', '$enemy_frozenking_p3',
]

# FindAllTrophies: every trophy item (one per name) minus excluded ones. Counted from the item pick-up stats.
TROPHIES = [
    '$enemy_kvastur', '$item_trophy_abomination', '$item_trophy_asksvin', '$item_trophy_barka', '$item_trophy_bjorn',
    '$item_trophy_bjorn_undead', '$item_trophy_blob', '$item_trophy_blob_frost', '$item_trophy_blob_lava',
    '$item_trophy_blob_morkhalla', '$item_trophy_boar', '$item_trophy_bonemass', '$item_trophy_bonemaw',
    '$item_trophy_brutebro', '$item_trophy_charredarcher', '$item_trophy_charredmage', '$item_trophy_charredmelee',
    '$item_trophy_cultist', '$item_trophy_cultist_hildir', '$item_trophy_deathsquito', '$item_trophy_deer',
    '$item_trophy_dragonqueen', '$item_trophy_draugr', '$item_trophy_draugrelite', '$item_trophy_dvergr',
    '$item_trophy_eikthyr', '$item_trophy_elaking', '$item_trophy_elder', '$item_trophy_fader',
    '$item_trophy_fallenvalkyrie', '$item_trophy_fenring', '$item_trophy_ghost', '$item_trophy_gjall',
    '$item_trophy_goblin', '$item_trophy_goblinbrute', '$item_trophy_goblinking', '$item_trophy_goblinshaman',
    '$item_trophy_greydwarf', '$item_trophy_greydwarfbrute', '$item_trophy_greydwarfshaman', '$item_trophy_growth',
    '$item_trophy_hare', '$item_trophy_hatchling', '$item_trophy_jotunwarrior', '$item_trophy_jotunwitch',
    '$item_trophy_leech', '$item_trophy_lox', '$item_trophy_mole', '$item_trophy_moose', '$item_trophy_morgen',
    '$item_trophy_neck', '$item_trophy_seal', '$item_trophy_seeker', '$item_trophy_seeker_brute',
    '$item_trophy_seekerqueen', '$item_trophy_serpent', '$item_trophy_sgolem', '$item_trophy_shamanbro',
    '$item_trophy_skeleton', '$item_trophy_skeleton_hildir', '$item_trophy_skeletonpoison', '$item_trophy_surtling',
    '$item_trophy_tick', '$item_trophy_ulv', '$item_trophy_volture', '$item_trophy_wolf', '$item_trophy_wraith',
    '$item_trophy_writhan',
]

# AllWeaponCraft: every weapon (1h, 2h, bows, torches) with a recipe, minus excluded items. Hand recipes with no
# station count here.
CRAFTABLE_WEAPONS = [
    '$item_atgeir_blackmetal', '$item_atgeir_bronze', '$item_atgeir_gold', '$item_atgeir_gold_bloodlightning',
    '$item_atgeir_gold_frostfire', '$item_atgeir_himminafl', '$item_atgeir_iron', '$item_axe2h_gold',
    '$item_axe2h_gold_bloodlightning', '$item_axe2h_gold_frostfire', '$item_axe_berzerkr', '$item_axe_berzerkr_blood',
    '$item_axe_berzerkr_lightning', '$item_axe_berzerkr_nature', '$item_axe_blackmetal', '$item_axe_bronze',
    '$item_axe_early', '$item_axe_flint', '$item_axe_gold', '$item_axe_gold_bloodlightning', '$item_axe_gold_frostfire',
    '$item_axe_iron', '$item_axe_jotunbane', '$item_axe_stone', '$item_battleaxe', '$item_battleaxe_blackmetal',
    '$item_battleaxe_crystal', '$item_battleaxe_skullsplittur', '$item_bilebomb', '$item_bomb_dynamite',
    '$item_bombblob_frost', '$item_bombblob_lava', '$item_bombblob_morkhalla', '$item_bombblob_poison',
    '$item_bombblob_poisonelite', '$item_bombblob_tar', '$item_bow', '$item_bow_ashlands', '$item_bow_ashlandsblood',
    '$item_bow_ashlandsroot', '$item_bow_ashlandsstorm', '$item_bow_draugrfang', '$item_bow_finewood', '$item_bow_gold',
    '$item_bow_gold_bloodlightning', '$item_bow_gold_frostfire', '$item_bow_huntsman', '$item_bow_snipesnap',
    '$item_club', '$item_crossbow_arbalest', '$item_crossbow_bloodlightning_gold', '$item_crossbow_frostfire_gold',
    '$item_crossbow_gold', '$item_crossbow_ripper', '$item_crossbow_ripper_blood', '$item_crossbow_ripper_lightning',
    '$item_crossbow_ripper_nature', '$item_fistweapon_bjorn', '$item_fistweapon_bjorn_undead',
    '$item_fistweapon_fenris', '$item_fistweapon_frostfire_gold', '$item_fistweapon_gold',
    '$item_fistweapon_gold_bloodlightning', '$item_graplinghook', '$item_knife_blackmetal', '$item_knife_butcher',
    '$item_knife_chitin', '$item_knife_copper', '$item_knife_flint', '$item_knife_gold',
    '$item_knife_gold_bloodlightning', '$item_knife_gold_frostfire', '$item_knife_silver', '$item_knife_skollandhati',
    '$item_mace2h_gold_bloodlightning', '$item_mace2h_gold_frostfire', '$item_mace_bronze', '$item_mace_eldner',
    '$item_mace_eldner_blood', '$item_mace_eldner_lightning', '$item_mace_eldner_nature', '$item_mace_gold',
    '$item_mace_gold_bloodlightning', '$item_mace_gold_frostfire', '$item_mace_iron', '$item_mace_needle',
    '$item_mace_silver', '$item_oozebomb', '$item_pickaxe_antler', '$item_pickaxe_blackmetal', '$item_pickaxe_bronze',
    '$item_pickaxe_iron', '$item_scythe', '$item_sledge_demolisher', '$item_sledge_gold', '$item_sledge_iron',
    '$item_spear_ancientbark', '$item_spear_bronze', '$item_spear_carapace', '$item_spear_chitin', '$item_spear_flint',
    '$item_spear_gold', '$item_spear_gold_bloodlightning', '$item_spear_gold_frostfire', '$item_spear_splitner',
    '$item_spear_splitner_blood', '$item_spear_splitner_lightning', '$item_spear_splitner_nature',
    '$item_spear_wolffang', '$item_staff_frostorbs', '$item_staff_lightning', '$item_staff_orbofahri',
    '$item_staff_spiritcaller', '$item_staff_thunderblood', '$item_staffclusterbomb', '$item_stafffireball',
    '$item_staffgreenroots', '$item_stafficeshards', '$item_staffredtroll', '$item_staffshield', '$item_staffskeleton',
    '$item_stagbreaker', '$item_sword2h_gold', '$item_sword2h_gold_bloodlightning', '$item_sword2h_gold_frostfire',
    '$item_sword_blackmetal', '$item_sword_bronze', '$item_sword_dyrnwyn', '$item_sword_gold',
    '$item_sword_gold_bloodlightning', '$item_sword_gold_frostfire', '$item_sword_iron', '$item_sword_krom',
    '$item_sword_mistwalker', '$item_sword_niedhogg', '$item_sword_niedhogg_blood', '$item_sword_niedhogg_lightning',
    '$item_sword_niedhogg_nature', '$item_sword_silver', '$item_sword_slayer', '$item_sword_slayer_blood',
    '$item_sword_slayer_lightning', '$item_sword_slayer_nature', '$item_sword_wood', '$item_torch',
]

# AllFoodCooked: every food with a recipe, plus every food a cooking station/oven can produce.
COOKED_FOOD = [
    '$item_asksvin_meat_cooked', '$item_bakedpoteitr', '$item_bakedpoteitr_uncooked', '$item_bjorn_meat_cooked',
    '$item_blacksoup', '$item_bloodpudding', '$item_blubber_cooked', '$item_boar_meat_cooked', '$item_boarjerky',
    '$item_bonemawmeat_cooked', '$item_bread', '$item_breaddough', '$item_bug_meat_cooked', '$item_carrotsoup',
    '$item_chicken_meat_cooked', '$item_deer_meat_cooked', '$item_deerstew', '$item_egg_cooked', '$item_eyescream',
    '$item_fierysvinstew', '$item_fish_cooked', '$item_fish_raw', '$item_fishandbread', '$item_fishandbreaduncooked',
    '$item_fishsoup', '$item_fishwraps', '$item_hare_meat_cooked', '$item_honeyglazedchicken',
    '$item_honeyglazedchickenuncooked', '$item_kalechips', '$item_kalechips_uncooked', '$item_lingondricka',
    '$item_loxmeat_cooked', '$item_loxpie', '$item_loxpie_uncooked', '$item_magicallystuffedmushroom',
    '$item_magicallystuffedmushroomuncooked', '$item_marinatedgreens', '$item_mashedmeat',
    '$item_meatballsmashedpoteitr', '$item_meatplatter', '$item_meatplatteruncooked', '$item_mincemeatsauce',
    '$item_mistharesupreme', '$item_mistharesupremeuncooked', '$item_moose_meat_cooked', '$item_moosekebab',
    '$item_mushroomomelette', '$item_necktailgrilled', '$item_oatmeallingonberryjam', '$item_oatmilk',
    '$item_onionsoup', '$item_ovenpancake', '$item_ovenpancake_uncooked', '$item_pancakes', '$item_piquantpie',
    '$item_piquantpie_uncooked', '$item_pulledbear', '$item_queensjam', '$item_roastedcrustpie',
    '$item_roastedcrustpie_uncooked', '$item_salad', '$item_sausages', '$item_scorchingmedley', '$item_sealsoup',
    '$item_seekeraspic', '$item_serpentmeatcooked', '$item_serpentstew', '$item_shocklatesmoothie',
    '$item_sizzlingberrybroth', '$item_smokedfish', '$item_smokedmoosemeat', '$item_sparklingshroomshake',
    '$item_spicymarmalade', '$item_turnipstew', '$item_vikingcupcake', '$item_vikingcupcake_uncooked',
    '$item_volture_meat_cooked', '$item_wolf_meat_cooked', '$item_wolf_skewer', '$item_wolfjerky',
    '$item_yggdrasilporridge',
]

# AllMiniBosses (any difficulty): the five Hildir mini-bosses and Dyrnwyn-related fights
MINIBOSSES = [
    '$enemy_fenringcultist_hildir', '$enemy_skeletonfire', '$enemy_charred_melee_Dyrnwyn',
    '$enemy_goblinbrute_hildircombined', '$enemy_goblin_hildir',
]

# GrindFish: every fish species must be caught (reeled in) once. Counted in the pickables table, not by picking the
# fish up.
FISH = {
    '$animal_fish1': 'Perch', '$animal_fish2': 'Pike', '$animal_fish3': 'Tuna', '$animal_fish4': 'Tetra',
    '$animal_fish5': 'Trollfish', '$animal_fish6': 'Giant Herring', '$animal_fish7': 'Grouper',
    '$animal_fish8': 'Coral Cod', '$animal_fish9': 'Anglerfish', '$animal_fish10': 'Northern Salmon',
    '$animal_fish11': 'Magmafish', '$animal_fish12': 'Pufferfish',
}

# Death achievements, from the game's Achievement assets (stat -> index into the 205 PlayerStatType values).
# "DeathByAllTypes" needs all 8 of these kinds at least once.
DEATH_KINDS = {"EnemyHit": 56, "Fall": 58, "Drowning": 59, "Burning": 60,
               "Freezing": 61, "Poisoned": 62, "Smoke": 63, "EdgeOfWorld": 65}
# "DeathByTree" needs DeathByTree (index 68). "DeathByTreeAll" needs a death by each of these 8 tree varieties.
DEATH_BY_TREE = 68
TREE_VARIETIES = {"Fir": 193, "Oak": 194, "Pine": 195, "Ashlands": 196,
                  "Beech": 199, "Birch": 200, "SnowFir": 201, "SnowPine": 202}

SUPPORTED_VERSION = 46

# Where each platform keeps the character file, for /valheim progress with no file attached.
# key -> (button label, emoji, guide text in Discord markdown)
FIND_GUIDES = {
    "windows": ("Windows", "🪟", (
        "Your character is a file named after them, e.g. `Ingrid.fch`.\n\n"
        "1. Press **Win+R**, paste this and press Enter:\n"
        "```\n%USERPROFILE%\\AppData\\LocalLow\\IronGate\\Valheim\\characters_local\n```\n"
        "2. Your `.fch` is in that folder. Not there? Try `…\\Valheim\\characters` (older or Steam Cloud "
        "characters), or the Steam Cloud copy in "
        "`C:\\Program Files (x86)\\Steam\\userdata\\<number>\\892970\\remote\\characters`.\n"
        "3. In Discord, run `/valheim progress`, click **save** and pick the file "
        "(or drag it from File Explorer onto the box).")),
    "linux": ("Linux", "🐧", (
        "Your character is a file named after them, e.g. `Ingrid.fch`. The quickest way to find it, in a terminal:"
        "\n```\nfind ~ -name \"*.fch\" 2>/dev/null\n```\n"
        "Usually one of these:\n"
        "• **Native Linux version** (Steam's default): `~/.config/unity3d/IronGate/Valheim/characters_local/`\n"
        "• **Proton** (the Windows version): `~/.local/share/Steam/steamapps/compatdata/892970/pfx/drive_c/users/"
        "steamuser/AppData/LocalLow/IronGate/Valheim/characters_local/`\n"
        "• **Steam Cloud**: `~/.local/share/Steam/userdata/<number>/892970/remote/characters/`\n"
        "• **Flatpak Steam**: the same, under `~/.var/app/com.valvesoftware.Steam/`\n\n"
        "Those folders are hidden: in Discord's file picker press **Ctrl+H** (show hidden) or **Ctrl+L** and paste "
        "the path, or copy the file to your Desktop first.")),
    "deck": ("Steam Deck", "🎮", (
        "1. Switch to **Desktop Mode** (Steam button → Power → Switch to Desktop).\n"
        "2. Open **Dolphin** (the file manager), press **Ctrl+H** to show hidden folders, and go to\n"
        "```\n/home/deck/.config/unity3d/IronGate/Valheim/characters_local/\n```\n"
        "3. Your character is the `.fch` named after them, e.g. `Ingrid.fch`. Open Discord in desktop mode (or in "
        "a browser) and upload it with `/valheim progress`. Using Proton instead? See the Linux guide.")),
    "mac": ("macOS", "🍎", (
        "1. In Finder press **Cmd+Shift+G**, paste this and press Enter:\n"
        "```\n~/Library/Application Support/IronGate/Valheim/characters_local\n```\n"
        "2. Your character is the `.fch` named after them, e.g. `Ingrid.fch`. Not there? Try "
        "`…/Valheim/characters`.\n"
        "3. Upload it with `/valheim progress` (drag it from Finder onto the **save** box).")),
    "console": ("Xbox / Game Pass", "🎯", (
        "Console players can't get at their character file, so this only works on PC.\n\n"
        "The **Game Pass / Microsoft Store** PC version stores characters in an encrypted container with random "
        "file names, not as a `.fch`, so it can't be read either. **Steam** (Windows, Linux, Steam Deck) and "
        "**macOS** work.")),
}
FIND_TIPS = ("**Before you upload:** quit to the main menu or exit the game, so the file has your latest progress. "
             "Upload `Name.fch`, not `Name.fch.old` (that's the previous save).")


class Reader:
    def __init__(self, data, pos=0):
        self.d, self.p = data, pos

    def i32(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def string(self):
        n = shift = 0
        while True:
            b = self.d[self.p]
            self.p += 1
            n |= (b & 0x7F) << shift
            shift += 7
            if b < 0x80:
                break
        s = self.d[self.p:self.p + n].decode("utf8")
        self.p += n
        return s

    def sdict(self):
        out = {}
        n = self.i32()
        if not 0 <= n < 100000:
            raise ValueError("implausible table size")
        for _ in range(n):
            k = self.string()
            out[k] = struct.unpack_from("<f", self.d, self.p)[0]
            self.p += 4
        return out

    def slist(self):
        n = self.i32()
        if not 0 <= n < 100000:
            raise ValueError("implausible list size")
        return [self.string() for _ in range(n)]


def parse_bytes(raw: bytes) -> dict:
    """Parse a .fch file's bytes. Raises ValueError (or struct.error/IndexError) if it isn't one."""
    if len(raw) < 16:
        raise ValueError("too small to be a character file")
    length = struct.unpack_from("<i", raw, 0)[0]
    if not 16 <= length <= len(raw) - 4:
        raise ValueError("not a Valheim character file")
    data = raw[4:4 + length]
    r = Reader(data)
    version = r.i32()
    n_stats = r.i32()
    n_sets = r.i32()
    if not (0 < n_stats < 2000 and 0 < n_sets < 64):
        raise ValueError("not a Valheim character file")
    sets = []
    for _ in range(n_sets):
        st = {"stats": struct.unpack_from(f"<{n_stats}f", data, r.p)}
        r.p += 4 * n_stats
        st["worlds"] = r.sdict()
        r.sdict()                   # known world keys
        r.sdict()                   # known commands
        enemy = [r.sdict() for _ in range(r.i32())]
        st["kills"] = enemy[0] if enemy else {}   # MixedAndTotal; 1-4 = unarmed, magic, ranged, melee
        st["picked_up"] = r.sdict()
        st["crafted"] = r.sdict()
        st["pickables"] = r.sdict()
        st["foods_eaten"] = r.sdict()
        st["pieces_built"] = r.sdict()
        sets.append(st)
    # player blob: first list of >100 "$..." strings = known recipes
    r.p = _find_recipes(data, r.p)
    recipes = r.slist()
    stations = {}
    for _ in range(r.i32()):
        k = r.string()
        stations[k] = r.i32()
    materials = r.slist()
    tutorials = r.slist()
    uniques = r.slist()
    trophies = r.slist()
    return {"version": version, "sets": sets, "recipes": recipes, "stations": stations,
            "materials": materials, "tutorials": tutorials, "uniques": uniques, "trophies": trophies}


def parse(path):
    with open(path, "rb") as f:
        return parse_bytes(f.read())


def _find_recipes(data, p):
    for q in range(p, len(data) - 8):
        n = struct.unpack_from("<i", data, q)[0]
        if 100 < n < 5000:
            try:
                r = Reader(data, q + 4)
                first = [r.string() for _ in range(3)]
                if all(s.startswith("$") for s in first):
                    return q
            except Exception:  # noqa: BLE001
                pass
    raise ValueError("player blob not found")


def strip(k):
    for pre in ("$enemy_", "$item_", "$piece_"):
        if k.startswith(pre):
            return k[len(pre):]
    return k


# Stat sets in the file, indexed by the game's DifficultyRequirement enum. Each kill/craft is added to set 0 (raw,
# includes cheated and pre-tracking data) and, when achievements are allowed, to set 1 (Any) and to every set from
# 3 (Casual) up to the difficulty it happened on. Achievements read these sets, never set 0.
SET_ANY, SET_NORMAL, SET_HARD = 1, 6, 7

# key -> (title, emoji)
SECTIONS = {
    "crafted": ("Items crafted", "⚒️"),
    "weapons": ("Weapons crafted", "⚔️"),
    "cooked": ("Food cooked", "🍲"),
    "built": ("Pieces built", "🏗️"),
    "deaths": ("Ways to die", "💀"),
    "tree-deaths": ("Killed by each tree", "🌲"),
    "enemies": ("Enemies killed", "🗡️"),
    "enemies-hard": ("Enemies killed (Hard)", "🔥"),
    "bosses": ("Bosses (Normal+)", "👑"),
    "bosses-hard": ("Bosses (Hard)", "☠️"),
    "minibosses": ("Mini-bosses", "🦹"),
    "fishing": ("Fish caught", "🎣"),
    "trophies": ("Trophies collected", "🏆"),
}


def _section(key, done, universe, label=strip, ignore=frozenset()):
    done_in = {k: v for k, v in done.items() if k in universe}
    return {"key": key, "title": SECTIONS[key][0], "emoji": SECTIONS[key][1],
            "done": {label(k): int(v) for k, v in sorted(done_in.items())},
            "missing": [label(k) for k in sorted(set(universe) - set(done))],
            "total": len(set(universe)),
            "extra": sorted(set(done) - set(universe) - set(ignore))}


def report(save: dict, wanted=None) -> list:
    """Progress per section: [{"key", "title", "emoji", "done": {name: count}, "missing": [names], "total",
    "extra": [tokens in the save that the built-in lists don't know]}]."""
    sets = save["sets"]
    anyd = sets[SET_ANY] if len(sets) > SET_ANY else sets[0]
    normal = sets[SET_NORMAL] if len(sets) > SET_NORMAL else anyd
    hard = sets[SET_HARD] if len(sets) > SET_HARD else anyd
    enemies, bosses = set(ENEMIES), set(BOSSES)
    stats = anyd["stats"]

    def stat(i):
        return stats[i] if i < len(stats) else 0

    out = []
    for key in wanted or SECTIONS:
        if key == "crafted":
            out.append(_section(key, anyd["crafted"], set(CRAFTABLE), ignore=set(anyd["crafted"])))
        elif key == "weapons":
            out.append(_section(key, anyd["crafted"], set(CRAFTABLE_WEAPONS), ignore=set(anyd["crafted"])))
        elif key == "cooked":
            out.append(_section(key, anyd["crafted"], set(COOKED_FOOD), ignore=set(anyd["crafted"])))
        elif key == "built":
            out.append(_section(key, anyd["pieces_built"], set(BUILDABLE), ignore=set(anyd["pieces_built"])))
        elif key == "deaths":
            died = {k: stat(i) for k, i in DEATH_KINDS.items() if stat(i) > 0}
            out.append(_section(key, died, set(DEATH_KINDS), label=lambda k: k))
        elif key == "tree-deaths":
            died = {k: stat(i) for k, i in TREE_VARIETIES.items() if stat(i) > 0}
            out.append(_section(key, died, set(TREE_VARIETIES), label=lambda k: k))
        elif key == "enemies":
            out.append(_section(key, anyd["kills"], enemies, ignore=bosses | {"$piece_trainingdummy"}))
        elif key == "enemies-hard":
            out.append(_section(key, hard["kills"], enemies, ignore=bosses))
        elif key == "bosses":
            out.append(_section(key, normal["kills"], bosses, ignore=enemies))
        elif key == "bosses-hard":
            out.append(_section(key, hard["kills"], bosses, ignore=enemies))
        elif key == "minibosses":
            out.append(_section(key, anyd["kills"], set(MINIBOSSES), ignore=set(anyd["kills"])))
        elif key == "fishing":
            # fish only count when reeled in (pickables table); picking one up by hand does not
            out.append(_section(key, anyd["pickables"], set(FISH), label=lambda t: FISH[t],
                                ignore=set(anyd["pickables"])))
        elif key == "trophies":
            # the game counts a trophy once it has been picked up while achievements were allowed
            out.append(_section(key, anyd["picked_up"], set(TROPHIES), ignore=set(anyd["picked_up"])))
    return out


def worlds(save: dict) -> list:
    return list(save["sets"][0]["worlds"]) if save["sets"] else []


def render_text(save: dict, sections: list, full: bool = False, name: str = "") -> str:
    """The plain-text report: every section, what's missing, and with full=True what's done."""
    lines = [f"{name or 'Character'}: profile v{save['version']}, worlds: {', '.join(worlds(save)) or '-'}"]
    for s in sections:
        lines.append(f"\n== {s['title']}: {len(s['done'])} / {s['total']} ==")
        if full:
            lines += [f"  [x] {k:35s} x{v}" for k, v in s["done"].items()]
        lines += [f"  [ ] {k}" for k in s["missing"]]
        if s["extra"]:
            lines.append(f"  -- in save but not in the built-in lists ({len(s['extra'])}): {', '.join(s['extra'])}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Show Valheim character progress (crafting, kills, trophies) from a .fch save. "
                    "By default only what is still missing is listed.")
    ap.add_argument("save", help="path to the .fch file")
    ap.add_argument("-f", "--full", action="store_true", help="also list completed entries, with counts")
    ap.add_argument("-o", "--only", metavar="LIST", action="append",
                    help="only show these lists (comma-separated or repeat the option); one of: " + ", ".join(SECTIONS))
    args = ap.parse_args(argv)
    wanted = list(SECTIONS)
    if args.only:
        wanted = [x.strip() for part in args.only for x in part.split(",") if x.strip()]
        bad = [x for x in wanted if x not in SECTIONS]
        if bad:
            ap.error(f"unknown list(s): {', '.join(bad)} (choose from {', '.join(SECTIONS)})")
    try:
        s = parse(args.save)
    except FileNotFoundError:
        ap.error(f"file not found: {args.save}")
    except OSError as e:
        ap.error(f"cannot read {args.save}: {e.strerror or e}")
    except (struct.error, IndexError, UnicodeDecodeError, AssertionError, ValueError):
        ap.error(f"{args.save} is not a supported Valheim .fch character file (profile version {SUPPORTED_VERSION})")
    sys.stdout.write(render_text(s, report(s, wanted), args.full, args.save))


if __name__ == "__main__":
    main()
