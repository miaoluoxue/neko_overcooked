import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).parent / 'neko'))
from cookbook import Item, Knowledge, Op, DishFlow, derive, resolve_leaf
from engine import Engine

class RegressionTests(unittest.TestCase):
    def test_unreachable_burnt_pot_releases_cleanup_job(self):
        e=Engine.__new__(Engine);e.log=Mock();e.cid=1;e.board=Mock()
        e.board.claim_stove.return_value=True
        pot=SimpleNamespace(name='utensil_frying_pan_01',state='Burnt',inside='Meat',x=15,z=15)
        holder=SimpleNamespace(id='hob',name='Hob',x=15,z=15)
        km=SimpleNamespace(cooking=[pot],stations={'hob':holder})
        e.pos=Mock(return_value=(2,12,''));e._fire_targets=Mock(return_value=[])
        e.state=Mock(return_value={});e.map=Mock(return_value=km)
        e._approach=Mock(return_value=False);e.interact=Mock()
        with patch('engine.time.time',return_value=100):
            self.assertFalse(e.clear_burnt_pot(km,{}))
        self.assertIsNone(e._burnt_cleanup)
        e.board.release_stove.assert_called_once_with('cleanup:'+pot.name,1)
        e.interact.assert_not_called()
        self.assertGreater(e._burnt_retry_at,100)

    def test_delivery_cooldown_rechecks_changed_counter_position(self):
        e=Engine.__new__(Engine);e._step_bench={'serve_any Meal':120}
        e._delivery_failed_at={'Meal':('Counter',1,1,100)}
        op=Op('serve_any','Meal',at_name='Counter',at_x=4,at_z=1)
        with patch('engine.CHORE_MODE','enroute'),patch('engine.time.time',return_value=101):
            self.assertFalse(e._chore_admitted(op,1,[],{})[0])
        with patch('engine.CHORE_MODE','enroute'),patch('engine.time.time',return_value=103):
            stationary=Op('serve_any','Meal',at_name='Counter',at_x=1.2,at_z=1)
            self.assertFalse(e._chore_admitted(stationary,1,[],{})[0])
            self.assertTrue(e._chore_admitted(op,1,[],{})[0])
            self.assertNotIn('serve_any Meal',e._step_bench)

    def test_moving_counter_live_interaction_wins_over_refresh_limit(self):
        for moved_on in (2, 3):  # Arrival, then micro-alignment state read.
            with self.subTest(moved_on=moved_on):
                e=Engine.__new__(Engine);e.log=Mock()
                old=SimpleNamespace(id='c',name='Counter',x=4,z=5)
                moved=SimpleNamespace(id='c',name='Counter',x=5,z=5)
                km=SimpleNamespace(stations={'c':old})
                live=SimpleNamespace(stations={'c':moved})
                e.state=Mock(return_value={'inRound':True})
                e.map=Mock(side_effect=lambda st: live if e.map.call_count>=moved_on else km)
                e.pos=Mock(return_value=(4,6,''));e.terrain=Mock(return_value=None)
                e._aim_ok=Mock(side_effect=lambda st,want,**kw:kw['km'] is live)
                e._avoid_cells=Mock(return_value=set());e._stand_cells=Mock(return_value=[(4,6)])
                e._reserve_cell=Mock();e.navigate_smart=Mock(return_value=True);e.face=Mock()
                e.interaction_targets=Mock(return_value=('',''));e.handle_target=Mock(return_value='')
                self.assertTrue(e._approach(km,4,5,want='Counter',_refreshes=2))
                self.assertEqual(e.navigate_smart.call_count,1)
                self.assertEqual(e._aim_ok.call_args.kwargs['at'],(5,5))

    def test_delivery_retries_after_cooldown_despite_previous_attempts(self):
        from engine import chore_key
        e=Engine.__new__(Engine);e.step_benched=Mock(return_value=False)
        op=Op('serve_any','Meal',at_x=1,at_z=1)
        with patch('engine.CHORE_MODE','enroute'):
            self.assertTrue(e._chore_admitted(op,1,[],{chore_key(op):10})[0])
            e.step_benched.return_value=True
            self.assertFalse(e._chore_admitted(op,1,[],{})[0])
        with patch('engine.CHORE_MODE','0'):
            e.step_benched.return_value=False
            self.assertFalse(e._chore_admitted(op,1,[],{})[0])

    def test_micro_alignment_stops_when_tracked_counter_moves(self):
        e=Engine.__new__(Engine);e.log=Mock()
        old=SimpleNamespace(id='c',name='Counter',x=4,z=5)
        moved=SimpleNamespace(id='c',name='Counter',x=8,z=5)
        km=SimpleNamespace(stations={'c':old});live=SimpleNamespace(stations={'c':moved})
        e.state=Mock(return_value={'inRound':True})
        e.map=Mock(side_effect=lambda st:km if e.map.call_count<=2 else live)
        e.pos=Mock(return_value=(4,6,''));e.terrain=Mock(return_value=None)
        e._aim_ok=Mock(return_value=False);e._avoid_cells=Mock(return_value=set())
        e._stand_cells=Mock(return_value=[(4,6)]);e._reserve_cell=Mock()
        e.navigate_smart=Mock(return_value=True);e.face=Mock()
        e.interaction_targets=Mock(return_value=('',''));e.handle_target=Mock(return_value='')
        e.speed=4;e._key=Mock(return_value='S');e.checkpoint=Mock(return_value=(False,1))
        with patch('bridge.keyboard_input.key_down') as down, patch('bridge.keyboard_input.key_up'), patch('engine.time.sleep'):
            self.assertFalse(e._approach(km,4,5,want='Counter',_refreshes=2))
        down.assert_not_called()

    def test_received_uncut_ingredient_completes_fetch_without_skipping_cook(self):
        e=Engine.__new__(Engine);e.log=Mock();e.know=Knowledge(items=[])
        e.pos=Mock(return_value=(0,0,'Pasta'))
        ops=[Op('fetch','Pasta'),Op('cook','Pasta'),Op('assemble','Pasta')]
        self.assertEqual(e._accept_returned_chop({},ops,[0,1,2]),[1,2])
        e.pos.return_value=(0,0,'Mushroom')
        self.assertEqual(e._accept_returned_chop({},ops,[0,1,2]),[0,1,2])
        e.pos.return_value=(0,0,'Pasta')
        self.assertEqual(e._accept_returned_chop({},ops,[1,2]),[1,2])

    def test_delivery_chore_includes_other_live_recipe_and_uses_its_flow(self):
        e=Engine.__new__(Engine);e.pos=Mock(return_value=(1,1,''));e._dyn=Mock(return_value={})
        current=SimpleNamespace(name='Meat');ready=SimpleNamespace(name='Mushroom')
        spot=SimpleNamespace(id='c',name='Counter',x=2,z=3)
        km=SimpleNamespace(of=lambda sem:[],stations={})
        e._all_flows=Mock(return_value=[(current,1),(ready,1),(ready,.5)])
        e._find_ready_dish=Mock(side_effect=lambda km,f:[spot] if f is ready else [])
        choices=e._chore_candidates(km,{},current)
        self.assertEqual([(o.action,o.target) for o in choices],[('serve_any','Mushroom')])
        e.pos.return_value=(1,1,'Plate');e._is_plate=Mock(return_value=True)
        e._deliver_plate=Mock(return_value=True)
        self.assertTrue(e.op_serve_any(km,1,1,choices[0],{},current))
        self.assertIs(e._deliver_plate.call_args.args[-1],ready)
        e._all_flows.return_value=[(current,1)]
        e._deliver_plate.reset_mock()
        self.assertFalse(e.op_serve_any(km,1,1,choices[0],{},current))
        e._deliver_plate.assert_not_called()

    def test_approach_accepts_live_target_without_chasing_grid_center(self):
        e=Engine.__new__(Engine);e.log=Mock()
        spot=SimpleNamespace(id='counter',name='Counter',x=4,z=5)
        km=SimpleNamespace(stations={'counter':spot})
        e.state=Mock(return_value={'inRound':True});e.map=Mock(return_value=km)
        e.pos=Mock(return_value=(4,6,''));e.terrain=Mock(return_value=None)
        e._aim_ok=Mock(return_value=True)
        e._avoid_cells=Mock(return_value=set());e._stand_cells=Mock(return_value=[])
        e.navigate_smart=Mock(return_value=False)
        self.assertTrue(e._approach(km,4,5,want='Counter'))
        e.navigate_smart.assert_not_called()
        e._aim_ok.return_value=False
        self.assertFalse(e._approach(km,4,5,want='Counter'))
        e.navigate_smart.assert_called_once()

    def test_ferry_cycles_blocked_counters_instead_of_repeating_nearest(self):
        e=Engine.__new__(Engine);e.log=Mock();e.pos=Mock(return_value=(0,0,'plate'))
        e.placement_target=Mock(return_value='')
        spots=[SimpleNamespace(id=str(i),name=str(i),x=i,z=0,on=[]) for i in range(3)]
        e._ferry_moving={s.id for s in spots}
        km=SimpleNamespace(of=lambda *a:spots,sorted_by_dist=lambda *a:spots)
        e._approach=Mock(return_value=False)
        for _ in range(4):
            self.assertFalse(e._ferry_ready_plate(km,{},None))
        self.assertEqual([c.kwargs['want'] for c in e._approach.call_args_list],['0','1','2','0'])

    def test_ferry_prefers_live_dock_even_if_previously_attempted(self):
        e=Engine.__new__(Engine);e.log=Mock();e.pos=Mock(return_value=(0,0,'plate'))
        spots=[SimpleNamespace(id=str(i),name=str(i),x=i,z=0,on=[]) for i in range(3)]
        e._ferry_moving={'0','1','2'};e._ferry_attempted={'2'}
        e.placement_target=Mock(return_value='2');e._approach=Mock(return_value=False)
        km=SimpleNamespace(of=lambda *a:spots,sorted_by_dist=lambda *a:spots)
        self.assertFalse(e._ferry_ready_plate(km,{},None))
        self.assertEqual(e._approach.call_args.kwargs['want'],'2')
        # A live target occupied by another plate must not be selected.
        spots[2].on=['plate'];e._ferry_attempted=set()
        self.assertFalse(e._ferry_ready_plate(km,{},None))
        self.assertEqual(e._approach.call_args.kwargs['want'],'0')

    def test_finished_plate_ferry_requires_observed_motion_and_verified_landing(self):
        e=Engine.__new__(Engine);e.log=Mock();e._unbind_plate_of=Mock()
        e.placement_target=Mock(return_value='')
        spot=SimpleNamespace(id='counter1',name='Counter',x=0,z=0,on=[])
        km=SimpleNamespace(of=lambda *a:[spot],sorted_by_dist=lambda *a:[spot],stations={'counter1':spot})
        flow=SimpleNamespace(name='Meal')
        before={'held':'equipment_plate_01'};after={'held':''}
        e.pos=Mock(side_effect=lambda s:(0,0,s['held']))
        e._approach=Mock(return_value=True);e._align_for_place=Mock(return_value=True)
        e.interact=Mock(return_value=True);e.map=Mock(return_value=km)
        e._spot_still_ready=Mock(return_value=True)
        self.assertFalse(e._ferry_ready_plate(km,before,flow))
        e.interact.assert_not_called()
        spot.x=2;e.state=Mock(side_effect=[before,after])
        self.assertTrue(e._ferry_ready_plate(km,before,flow))
        self.assertEqual(e._ferry_waiting,('Meal','counter1'))
        e._unbind_plate_of.assert_called_once_with(flow)
        e._unbind_plate_of.reset_mock();e._spot_still_ready.return_value=False
        e.state=Mock(side_effect=[before,after])
        self.assertFalse(e._ferry_ready_plate(km,before,flow))
        e._unbind_plate_of.assert_not_called()
        spot.on=['OtherPlate'];e.interact.reset_mock()
        self.assertFalse(e._ferry_ready_plate(km,before,flow))
        e.interact.assert_not_called()

    def test_recipe_preserves_distinct_cooking_methods_for_same_ingredient(self):
        tree={'k':'comp','i':[{'k':'cook','cookId':cid,'i':[{'k':'ing','n':'Fish'}]} for cid in (11,22)]}
        flow=derive({'name':'Meal','tree':tree},Knowledge([]))
        self.assertEqual([o.cook_id for o in flow.ops if o.action=='cook'],[11,22])

    def test_stove_and_existing_food_must_match_recipe_cooking_method(self):
        e=Engine.__new__(Engine);e.board=None;e._cook_req=Mock(return_value=('',[11,22]))
        pot=SimpleNamespace(id='pot',pot_name=lambda:'pot')
        pan=SimpleNamespace(id='pan',pot_name=lambda:'pan')
        vessels={'pot':SimpleNamespace(cook_id=11,busy=False,is_pot=True,inside='Fish'),
                 'pan':SimpleNamespace(cook_id=22,busy=False,is_pot=True,inside='Fish')}
        km=SimpleNamespace(sorted_by_dist=lambda *a:[pot,pan],cooking_on=lambda s:vessels[s.id])
        self.assertIs(e._pick_stove(km,0,0,True,ing='Fish',cook_id=22)[0],pan)
        self.assertIs(e._find_pot_with(km,0,0,'Fish',cook_id=22)[0],pan)
        self.assertIsNone(e._pick_stove(km,0,0,True,ing='Fish',cook_id=33)[0])
        self.assertIs(e._pick_stove(km,0,0,True,ing='Fish')[0],pot)

    def test_chopping_reacquires_live_board_only_with_matching_raw_food(self):
        e=Engine.__new__(Engine);e.log=Mock()
        e.know=Knowledge([Item(name='Fish',next='UncookedFish'),Item(name='ChoppedFish',ing='UncookedFish')])
        old=SimpleNamespace(id='board0',x=0,z=0,name='Board',on=['Fish'])
        live=SimpleNamespace(id='board0',x=5,z=8,name='Board',on=['Fish'])
        km=SimpleNamespace(stations={'board0':live})
        e.state=Mock(return_value={'inRound':True});e.map=Mock(return_value=km)
        e._approach=Mock(return_value=True)
        self.assertTrue(e._reapproach_chopping_board(old,'UncookedFish'))
        e._approach.assert_called_once_with(km,5,8,want='Board')
        e._approach.reset_mock();live.on=['ChoppedFish']
        self.assertFalse(e._reapproach_chopping_board(old,'UncookedFish'))
        e._approach.assert_not_called()

    def test_pot_plate_rejects_foreign_recipe_before_transfer(self):
        e=Engine.__new__(Engine);e.log=Mock()
        e._dish_sets=Mock(return_value=({'pasta','burritomeat'},set()))
        flow=object()
        self.assertTrue(e._plate_can_take({'pasta'},'BurritoMeat',flow))
        self.assertFalse(e._plate_can_take({'pasta','prawn'},'BurritoMeat',flow))
        self.assertFalse(e._plate_can_take({'burritomeat'},'BurritoMeat',flow))
        e.state=Mock(return_value={});e.pos=Mock(return_value=(0,0,'equipment_plate_01'))
        e._held_contents=Mock(return_value={'pasta','prawn'})
        e._put_down_plate=Mock(return_value=False)
        self.assertFalse(e._get_plate_for_pot(None,0,0,'',taking='BurritoMeat',flow=flow))
        e._put_down_plate.assert_called_once()

    def test_delivery_accepts_counter_increment_despite_replenished_same_named_order(self):
        e=Engine.__new__(Engine);e.log=Mock()
        serve=SimpleNamespace(id='serve0',name='Serve',x=1,z=0)
        km=SimpleNamespace(nearest=lambda *a:serve,stations={'serve0':serve})
        old={'held':'equipment_plate_01','round':{'seq':1,'scene':'kitchen','delivered':0}}
        new={'held':'','round':{'seq':1,'scene':'kitchen','delivered':1}}
        e.state=Mock(side_effect=lambda **kw:old if e.state.call_count==1 else new)
        e.pos=Mock(side_effect=lambda s:(0,0,s['held']))
        e._approach=Mock(return_value=True);e.face=Mock();e._align_for_place=Mock(return_value=True)
        e.live_orders=Mock(return_value=[{'name':'Meal'}]);e.chef=Mock(return_value={})
        e.interact=Mock(return_value=True);e._unbind_plate_of=Mock()
        e._held_contents=Mock(return_value=set());e.map=Mock(return_value=km);e._onhas_raw=Mock(return_value='')
        with patch('engine.time.sleep'),patch('engine.time.time',side_effect=[0,0,3]):
            self.assertTrue(e._deliver_plate(km,0,0,Op('deliver','Meal'),old))
        e._unbind_plate_of.assert_called_once()
        # A teammate's delivery must not count while this chef still holds a plate.
        new['held']='equipment_plate_01';e.state.reset_mock();e._unbind_plate_of.reset_mock()
        with patch('engine.time.sleep'),patch('engine.time.time',side_effect=[0,0,3]):
            self.assertFalse(e._deliver_plate(km,0,0,Op('deliver','Meal'),old))
        e._unbind_plate_of.assert_not_called()
        new['held']=''
        for seq, delivered in ((1,0),(2,1)):
            new['round'].update(seq=seq,delivered=delivered);e.state.reset_mock()
            with patch('engine.time.sleep'),patch('engine.time.time',side_effect=[0,0,3]):
                self.assertFalse(e._deliver_plate(km,0,0,Op('deliver','Meal'),old))

    def test_ready_dish_is_retained_when_delivery_route_is_temporarily_closed(self):
        e=Engine.__new__(Engine);e.log=Mock();e.wait_idle=Mock()
        e._ferry_ready_plate=Mock(return_value=False)
        e.pos=Mock(return_value=(0,0,'equipment_plate_01'))
        e._dish_sets=Mock(return_value=({'pasta','burritomeat'},set()))
        e._held_contents=Mock(return_value={'pasta','burritomeat'})
        e._deliver_plate=Mock(return_value=False)
        flow=SimpleNamespace(name='Pasta_MeatOnly_New')
        self.assertIs(e._deliver_ready_held(None,{},flow),False)
        e.wait_idle.assert_called_once_with(.5)
        e._deliver_plate.return_value=True
        self.assertIs(e._deliver_ready_held(None,{},flow),True)
        e._held_contents.return_value={'pasta'}
        e._deliver_plate.reset_mock()
        self.assertIsNone(e._deliver_ready_held(None,{},flow))
        e._deliver_plate.assert_not_called()

    def test_moving_stove_keeps_cooking_visible_through_old_reference(self):
        from map_model import KitchenMap
        old=SimpleNamespace(id='hob0',name='Stove',x=0,z=0)
        live=SimpleNamespace(id='hob0',name='Stove',x=0,z=6)
        pot=SimpleNamespace(name='utensil_pot_01',tag='CookingUtensil',on='Stove',x=0,z=6)
        cooked=SimpleNamespace(name=pot.name,x=0,z=6,state='Cooked')
        km=KitchenMap(stations={'hob0':live},items=[pot],cooking=[cooked])
        self.assertIs(km.pot_on(old),pot)
        self.assertIs(km.cooking_on(old),cooked)
        # The old-bridge spatial fallback must also use the live station.
        km.items=[]
        self.assertIs(km.cooking_on(old),cooked)
        # Do not accidentally associate cookware left at the departed position.
        pot.z=0;km.items=[pot];km.cooking=[]
        self.assertIsNone(km.pot_on(old))
        self.assertIsNone(km.cooking_on(old))

    def test_failed_approach_does_not_walk_directly_after_rejected_routes(self):
        e=Engine.__new__(Engine);e.log=Mock()
        km=SimpleNamespace(stations={})
        e._station_named=Mock(return_value=None)
        e.state=Mock(return_value={'inRound':True});e.map=Mock(return_value=km)
        e.pos=Mock(return_value=(0,0,''));e.terrain=Mock(return_value=SimpleNamespace(ok=True))
        e._avoid_cells=Mock(return_value=set());e._stand_cells=Mock(return_value=[(9,0),(10,1)])
        e._reserve_cell=Mock();e.navigate_smart=Mock(return_value=False)
        e.face=Mock();e._aim_ok=Mock(return_value=False)
        e.interaction_targets=Mock(return_value=('',''));e.handle_target=Mock(return_value='')
        e.speed=4;e._key=Mock(return_value='D');e.checkpoint=Mock(return_value=(False,10))
        with patch('bridge.keyboard_input.key_down') as down, \
                patch('bridge.keyboard_input.key_up'),patch('engine.time.sleep'):
            self.assertFalse(e._approach(km,10,0,want='Serve'))
        down.assert_not_called();e.face.assert_not_called()
        self.assertEqual(e.navigate_smart.call_count,2)

    def test_chopping_uses_observed_raw_to_ingredient_relation(self):
        e=Engine.__new__(Engine)
        e._preposed_ok=Mock(return_value=False)
        e.know=Knowledge([Item(name='Fish',next='UncookedFish',stages=8),
                          Item(name='ChoppedFish',ing='UncookedFish'),
                          Item(name='Prawn',next='Prawn',stages=8)])
        self.assertTrue(e._held_choppable('Fish','UncookedFish'))
        self.assertFalse(e._held_choppable('ChoppedFish','UncookedFish'))
        self.assertFalse(e._held_choppable('Fish','Prawn'))
        self.assertTrue(e._held_choppable('Prawn','Prawn'))
        op=Op('chop','UncookedFish')
        self.assertTrue(e._op_actionable(None,{},op,0,0,'Fish',strict=True)[0])
        self.assertFalse(e._op_actionable(None,{},op,0,0,'Prawn',strict=True)[0])
        self.assertTrue(e._handoff_matches('Fish',Op('pass','UncookedFish',handoff='chop')))
        e.know=Knowledge([Item(tag='Crate',spawn='Fish',spawnNext='UncookedFish')])
        self.assertTrue(e._held_choppable('Fish','UncookedFish'))

    def test_placement_faces_current_station_position_on_every_sample(self):
        e = Engine.__new__(Engine)
        old = SimpleNamespace(id='board0', name='Board', x=10, z=0)
        e._fresh_station_name = Mock(return_value='Board')
        e.state = Mock(return_value={'inRound': True})
        e.map = Mock(side_effect=[SimpleNamespace(stations={'board0':
            SimpleNamespace(id='board0', name='Board', x=x, z=0)}) for x in (.1,-.1,-.1)])
        e.pos = Mock(return_value=(0,0,'')); e.face = Mock()
        e.chef = Mock(side_effect=[{'placeh':''},{'placeh':''},{'placeh':'Board'}])
        e._key = Mock(return_value='D')
        with patch('engine.time.sleep'), patch('bridge.keyboard_input.key_down') as down, \
                patch('bridge.keyboard_input.key_up'):
            self.assertTrue(e._align_for_place(old, tries=3))
        self.assertEqual(e.face.call_args_list, [unittest.mock.call(.1,0),unittest.mock.call(-.1,0)])
        down.assert_not_called()

    def test_placement_discards_standing_cells_after_station_moves(self):
        e = Engine.__new__(Engine)
        old = SimpleNamespace(id='board0', name='Board', x=10, z=0)
        now = SimpleNamespace(id='board0', name='Board', x=20, z=0)
        moved = SimpleNamespace(id='board0', name='Board', x=24, z=0)
        e.terrain = Mock(return_value=SimpleNamespace(ok=True,cell_of=lambda x,z:(x,z)))
        e.state = Mock(return_value={'inRound':True})
        e.map = Mock(side_effect=[SimpleNamespace(stations={'board0':now}),
                                 SimpleNamespace(stations={'board0':moved})])
        e.pos = Mock(return_value=(0,0,'')); e._avoid_cells = Mock(return_value=set())
        e._stand_cells = Mock(return_value=[(19,0),(21,0)])
        e._reserve_cell=Mock();e.navigate_smart=Mock(return_value=True)
        e.face=Mock();e.log=Mock()
        self.assertFalse(e._align_try_other_cells(old,'Board'))
        self.assertEqual(e._stand_cells.call_args.args[1:3],(20,0))
        e.navigate_smart.assert_called_once();e.face.assert_not_called()

    def test_floor_recovery_respects_live_handoff_and_retries_after_expiry(self):
        e = Engine.__new__(Engine)
        e.log = Mock(); e.pos = Mock(return_value=(0, 0, ''))
        e._handoffs = {e.handoff_key('cook', 'Pasta'): (110, 'Pasta')}
        e._step_bench = {}
        tm = SimpleNamespace(ok=True, cell_of=lambda x,z:(x,z),
                             distances_from=lambda *a,**k:{(1,0):1})
        e.terrain = Mock(return_value=tm); e._travel_edges = Mock(return_value=[])
        e._held_cookable = Mock(return_value=True)
        e._is_unprocessed = Mock(return_value=False)
        e.op_fetch = Mock(return_value=True)
        item = SimpleNamespace(name='Pasta', x=1, z=0)
        km = SimpleNamespace(unseen_items=lambda:[item])
        ops = [Op('cook', 'Pasta')]
        with patch('engine.time.time', return_value=100):
            self.assertFalse(e._recover_ground_chop(km, {}, ops, [0]))
        e.op_fetch.assert_not_called()
        with patch('engine.time.time', return_value=111):
            self.assertTrue(e._recover_ground_chop(km, {}, ops, [0]))
        e.op_fetch.assert_called_once()
        # A chopped portion must not be reclaimed through the chop entry while
        # its downstream cook is cooling down; expiry re-enables recovery.
        e.op_fetch.reset_mock()
        e._step_bench = {'cook Pasta': 120}
        ops = [Op('chop', 'Pasta'), Op('cook', 'Pasta')]
        with patch('engine.time.time', return_value=115):
            self.assertFalse(e._recover_ground_chop(km, {}, ops, [0,1]))
        e.op_fetch.assert_not_called()
        with patch('engine.time.time', return_value=121):
            self.assertTrue(e._recover_ground_chop(km, {}, ops, [0,1]))
        e.op_fetch.assert_called_once()

    def test_live_disconnected_terrain_never_uses_static_shortcut(self):
        e=Engine.__new__(Engine);e.arrive=.8;e.log=Mock()
        tm=SimpleNamespace(ok=True,find_path=Mock(return_value=None))
        e.terrain=Mock(return_value=tm);e.state=Mock(return_value={'inRound':True})
        e.pos=Mock(return_value=(0,0,''));e.chef_y=Mock(return_value=0)
        e._dynamic_blocks=Mock(return_value=set());e._travel_edges=Mock(return_value={})
        e.session_station=Mock(return_value=None);e._has_pilot_console=Mock(return_value=False)
        e.pilot_bridge=Mock(return_value=False);e._game_path=Mock(return_value=[(10,0)])
        e._native_path_safe=Mock(return_value=[(10,0)]);e.navigate=Mock(return_value=True)
        self.assertFalse(e.navigate_smart(object(),10,0))
        e._game_path.assert_not_called();e.navigate.assert_not_called()
    def test_idle_chopper_processes_floor_stock_for_live_orders(self):
        e=Engine.__new__(Engine);e.log=Mock();e.pos=Mock(return_value=(0,0,''))
        e._mate=Mock(return_value=(6,0,'',0,False));e._travel_edges=Mock(return_value=[])
        tm=SimpleNamespace(ok=True,distances_from=lambda x,z,**kw:{(x,z):0})
        e.terrain=Mock(return_value=tm)
        flow=SimpleNamespace(ops=[Op('chop','Potato'),Op('cook','Potato')])
        e._all_flows=Mock(return_value=[(flow,.5)])
        e._op_target_for_score=Mock(return_value=((0,0),''));e._stand_cell_of=Mock(return_value=(0,0))
        e._find_ground_item=Mock(return_value=object());e.op_fetch=Mock(return_value=True)
        e.state=Mock(return_value={'inRound':True});e.map=Mock(return_value=object());e.op_chop=Mock()
        km=SimpleNamespace(items=[],of=lambda sem:[SimpleNamespace(x=6,z=0)] if sem=='fryer' else [])
        self.assertTrue(e._work_split_stock(km,{}))
        e.op_chop.assert_called_once()
        e.op_chop.reset_mock();e._find_ground_item.return_value=None
        self.assertFalse(e._work_split_stock(km,{}));e.op_chop.assert_not_called()
        # No empty board remains, but a finished portion on it must be removed.
        e.know=Knowledge([Item(name='Chopped_Potato',ing='Potato')])
        km.items=[SimpleNamespace(name='Chopped_Potato',x=0,z=0,on='board',carrier='',workable=False)]
        e._op_target_for_score.return_value=(None,'all boards occupied')
        self.assertTrue(e._work_split_stock(km,{}))
        self.assertEqual(e.op_fetch.call_args.args[3].target,'Chopped_Potato')
        e.pos.return_value=(0,0,'Chopped_Potato')
        km.of=lambda sem:[SimpleNamespace(x=6,z=0)] if sem=='fryer' else []
        e._ground_pass_plan=Mock(return_value=None);e.wait_idle=Mock()
        self.assertTrue(e._work_split_stock(km,{}))
        e.wait_idle.assert_called_once_with(.3)
        # The cooking island must not fetch stock intended for a teammate
        # whose disconnected island has no cooker to receive it.
        e.pos.return_value=(0,0,'');km.of=lambda sem:[]
        e.op_fetch.reset_mock()
        self.assertFalse(e._work_split_stock(km,{}))
        e.op_fetch.assert_not_called()

    def test_fetch_execution_prefers_reachable_floor_over_remote_crate_item(self):
        from engine import _GroundItem
        e=Engine.__new__(Engine);e.pos=Mock(return_value=(0,0,''));e.state=Mock(return_value={})
        e.terrain=Mock(return_value=None);e.log=Mock();e._approach=Mock(return_value=False)
        remote=SimpleNamespace(id='crate0',name='Crate',x=20,z=20)
        ground=_GroundItem(SimpleNamespace(name='Potato',x=1,z=0))
        e._find_item_station=Mock(return_value=remote);e._fetch_source_live=Mock(return_value=ground)
        km=SimpleNamespace(of=lambda kind:[],packs_worn=lambda:[])
        self.assertFalse(e.op_fetch(km,0,0,Op('fetch','Potato'),{}))
        self.assertEqual(e._approach.call_args.args[1:3],(1,0))

    def test_idle_supply_uses_order_tuples_and_counts_existing_stock(self):
        e=Engine.__new__(Engine);e.pos=Mock(return_value=(0,0,''));e._pots_live=Mock(return_value={})
        e.log=Mock();e._mate=Mock(return_value=(6,0,'',0,False));e._travel_edges=Mock(return_value=[])
        tm=SimpleNamespace(cell_of=lambda x,z:(x,z),distances_from=lambda *a,**k:{(6,0):0})
        e._ground_pass_plan=Mock(return_value=(tm,((0,0),(6,0))))
        e._reach_ok_pred=Mock(return_value=None);e._mate_can=Mock(return_value=(True,None,None,''))
        e._all_flows=Mock(return_value=[(SimpleNamespace(ops=[Op('chop','Potato')]),.5)])
        e.op_fetch=Mock(return_value=True);e.state=Mock(return_value={});e._ground_pass=Mock()
        km=SimpleNamespace(unseen_items=lambda:[],items=[],find_source=lambda *a,**k:object())
        e.map=Mock(return_value=km)
        self.assertTrue(e._stock_split_floor(km,{},None))
        e._ground_pass.assert_called_once()
        km.items=[SimpleNamespace(name='Potato')]
        e.op_fetch.reset_mock()
        self.assertFalse(e._stock_split_floor(km,{},None))
        e.op_fetch.assert_not_called()

    def test_ground_handoff_requires_observed_new_receiver_stock(self):
        e=Engine.__new__(Engine);e.log=Mock();e.navigate_smart=Mock();e.face=Mock()
        e._travel_edges=Mock(return_value=[]);e._my_player_index=Mock(return_value=0)
        tm=SimpleNamespace(world_of=lambda *c:c,cell_of=lambda x,z:(round(x),round(z)),
                           distances_from=lambda *a,**k:{(6,0):0})
        plan=(tm,((0,0),(6,0)));e._ground_pass_plan=Mock(return_value=plan)
        e.terrain=Mock(return_value=tm);e.state=Mock(return_value={'inRound':True})
        e.pos=Mock(side_effect=lambda s:(0,0,'Potato') if e.pos.call_count==1 else (0,0,''))
        e._mate=Mock(return_value=(6,0,'OtherFood',0,False))
        e.bridge=SimpleNamespace(direct=Mock(return_value={'ok':True}));e.mark_handoff=Mock()
        empty=SimpleNamespace(unseen_items=lambda:[])
        food=SimpleNamespace(name='Potato',x=6,z=0)
        landed=SimpleNamespace(unseen_items=lambda:[food])
        e.map=Mock(side_effect=[empty,landed,landed])
        with patch('engine.time.sleep'):
            self.assertTrue(e._ground_pass(empty,{},Op('pass','Potato',handoff='chop'),plan))
        e.mark_handoff.assert_called_once()
        # An RPC ack alone, or food on the sender's ground, is not delivery.
        e.pos.reset_mock();e.mark_handoff.reset_mock();food.x=0
        e.map=Mock(return_value=landed)
        with patch('engine.time.sleep'):
            self.assertFalse(e._ground_pass(empty,{},Op('pass','Potato',handoff='chop'),plan))
        e.mark_handoff.assert_not_called()
        e.pos.reset_mock();e.bridge.direct.reset_mock();e.face.return_value=False
        with patch('engine.time.sleep'):
            self.assertFalse(e._ground_pass(empty,{},Op('pass','Potato',handoff='chop'),plan))
        e.bridge.direct.assert_not_called()

    def test_floor_landing_requires_interior_receiver_ground(self):
        from ground_buffer import landing_plan
        tm=SimpleNamespace(world_of=lambda x,z:(x,z),cell_of=lambda x,z:(round(x),round(z)))
        own={(0,0):0,(1,0):1}
        other={(x,z):0 for x in (8,9,10) for z in (-1,0,1)}
        self.assertIsNotNone(landing_plan(tm,own,other))
        self.assertIsNone(landing_plan(tm,own,{(8,0):0}))
        self.assertIsNone(landing_plan(tm,own,{(12,0):0}))
        self.assertIsNone(landing_plan(tm,own,own))
        narrow={(x,z):0 for x in (8,9,10) for z in (0,1)}
        self.assertIsNotNone(landing_plan(tm,own,narrow))

    def test_processed_ground_stock_is_reused_but_not_raw_or_remote(self):
        e=Engine.__new__(Engine);e.pos=Mock(return_value=(0,0,''));e.log=Mock()
        e._handoffs = {}
        e._step_bench = {}
        e.know=Knowledge([Item(name='Potato',ing='Potato',next='Chopped_Potato'),
                          Item(name='Chopped_Potato',ing='Potato')])
        tm=SimpleNamespace(ok=True,cell_of=lambda x,z:(x,z),distances_from=lambda *a,**k:{(0,0):0})
        e.terrain=Mock(return_value=tm);e._travel_edges=Mock(return_value=[])
        e.op_fetch=Mock(return_value=True)
        it=SimpleNamespace(name='Chopped_Potato',workable=False,x=0,z=0)
        km=SimpleNamespace(unseen_items=lambda:[it]);ops=[Op('chop','Potato')]
        self.assertTrue(e._recover_ground_chop(km,{},ops,[0]))
        self.assertEqual(e.op_fetch.call_args.args[3].target,'Chopped_Potato')
        it.workable=True
        self.assertFalse(e._recover_ground_chop(km,{},ops,[0]))
        it.workable=False;it.z=9
        self.assertFalse(e._recover_ground_chop(km,{},ops,[0]))

    def test_recipe_fetch_keeps_food_until_its_processing_handoff(self):
        e=Engine.__new__(Engine)
        ops=[Op('chop','NuggetChicken'),Op('fetch','Potato')]
        for strict in (True,False):
            self.assertFalse(e._op_actionable(None,{},ops[1],0,0,'NuggetChicken',
                             strict=strict,ops=ops,pending=[0,1])[0])
            self.assertTrue(e._op_actionable(None,{},ops[1],0,0,'',
                             strict=strict,ops=ops,pending=[0,1])[0])

    def test_crate_child_is_not_a_rat_blocking_the_aisle(self):
        from map_model import KitchenMap, Station, Mover
        from terrain import TerrainMap
        t=TerrainMap({'w':5,'h':5,'grid':'.'*25,'cellx':1.2,'cellz':1.2})
        crate=Station(id='c',kind='PickupItemSpawner',sub='',name='DispenserCrate',x=0,z=0,spawn='Potato')
        child=Mover(name='NewCrate',kind='ByName',x=0,z=0,r=.6)
        km=KitchenMap(stations={'c':crate},movers=[child])
        self.assertEqual(km.blocked_by_movers(t),set())
        child.name='Rat'
        self.assertTrue(km.blocked_by_movers(t))
        child.name='NewCrate';child.kind='RespawnCollider'
        self.assertTrue(km.blocked_by_movers(t))

    def test_retired_handoff_does_not_wait_for_another_identical_order(self):
        e=Engine.__new__(Engine);e._handoffs={('cook','potato'):(100,'Potato')}
        e._pots_live=Mock(return_value={})
        old={'id':11,'name':'Chips'};new={'id':12,'name':'Chips'}
        f=DishFlow('Chips');f.slot=e._order_key(old)
        e.live_orders=Mock(return_value=[new])
        self.assertTrue(e._handoff_order_retired(f))
        e.live_orders.return_value=[old,new]
        self.assertFalse(e._handoff_order_retired(f))
        e.live_orders.return_value=[]
        self.assertFalse(e._handoff_order_retired(f))
        e.live_orders.return_value=[new];e._pots_live.return_value={'pot':1}
        self.assertFalse(e._handoff_order_retired(f))

    def test_face_uses_exact_analog_direction(self):
        e=Engine.__new__(Engine);e.state=Mock(return_value={'inRound':True})
        e.pos=Mock(return_value=(0,0,''));e.terrain=Mock(return_value=None)
        driver=Mock()
        with patch('bridge.keyboard_input.get_driver',return_value=driver),patch('engine.time.sleep'):
            self.assertTrue(e.face(3,4))
        driver.move.assert_called_once_with(.6,-.8)
        driver.release_all.assert_called_once()

    def test_sender_can_prepare_other_ingredients_while_chop_is_delegated(self):
        e=Engine.__new__(Engine);e._handoffs={('chop','potato'):(100,'Potato')}
        e.know=Knowledge(items=[])
        with patch('engine.time.time',return_value=50):
            self.assertTrue(e._op_actionable(None,{},Op('fetch','NuggetChicken'),0,0,'')[0])
            self.assertFalse(e._op_actionable(None,{},Op('fetch','Potato'),0,0,'')[0])
            e.know=Knowledge(items=[Item(name='Fish',next='UncookedFish')])
            e._handoffs={('chop','uncookedfish'):(100,'UncookedFish')}
            self.assertFalse(e._op_actionable(None,{},Op('fetch','Fish'),0,0,'')[0])
        with patch('engine.time.time',return_value=101):
            self.assertTrue(e._op_actionable(None,{},Op('fetch','Fish'),0,0,'')[0])

    def test_cook_handoff_blocks_same_stock_but_allows_other_ingredient(self):
        e=Engine.__new__(Engine);e.know=Knowledge(items=[])
        e._handoffs={('cook','burritomeat'):(100,'BurritoMeat')}
        with patch('engine.time.time',return_value=50):
            self.assertFalse(e._op_actionable(None,{},Op('fetch','BurritoMeat'),0,0,'')[0])
            self.assertTrue(e._op_actionable(None,{},Op('fetch','Pasta'),0,0,'')[0])
        with patch('engine.time.time',return_value=101):
            self.assertTrue(e._op_actionable(None,{},Op('fetch','BurritoMeat'),0,0,'')[0])

    def test_returned_fish_completes_renamed_raw_fetch(self):
        e=Engine.__new__(Engine);e.log=Mock();e._handoffs={}
        e.know=Knowledge(items=[Item(name='Fish',next='UncookedFish'),
                              Item(name='ChoppedFish',ing='UncookedFish')])
        e.pos=Mock(return_value=(0,0,'ChoppedFish'))
        ops=[Op('fetch','Fish'),Op('chop','UncookedFish'),Op('cook','UncookedFish')]
        self.assertEqual(e._accept_returned_chop({},ops,[0,1,2]),[2])

    def test_pass_needs_actual_receiver_receipt(self):
        e=Engine.__new__(Engine); e.state=Mock(return_value={'inRound':True})
        e._mate=Mock(return_value=(0,4,'',0,False))
        with patch('engine.time.sleep'):
            self.assertFalse(e._confirm_pass('Chopped_Potato'))
            e._mate.return_value=(0,4,'NuggetChicken',0,False)
            self.assertFalse(e._confirm_pass('Chopped_Potato'))
            e._mate.side_effect=[(0,4,'',0,False),(0,4,'Chopped_Potato',0,False)]
            self.assertTrue(e._confirm_pass('Chopped_Potato'))
            e._mate.side_effect=[(0,4,'',0,False)]*25+[(0,4,'Chopped_Potato',0,False)]
            self.assertTrue(e._confirm_pass('Chopped_Potato'))

    def test_caught_chopped_food_completes_only_one_preparation(self):
        e=Engine.__new__(Engine);e.log=Mock();e._handoffs={}
        e.know=Knowledge(items=[Item(name='Chopped_Potato',ing='Potato')])
        e.pos=Mock(return_value=(0,0,'Chopped_Potato'))
        ops=[Op('fetch','Potato'),Op('chop','Potato'),Op('cook','Potato'),
             Op('fetch','Potato'),Op('chop','Potato'),Op('cook','Potato')]
        self.assertEqual(e._accept_returned_chop({},ops,list(range(6))),[2,3,4,5])
        self.assertEqual(e._accept_returned_chop({},ops,[2,3,4,5]),[2,3,4,5])
        e.pos.return_value=(0,0,'Potato')
        self.assertEqual(e._accept_returned_chop({},ops,list(range(6))),list(range(6)))

    def test_cook_handoff_accepts_processed_ingredient_identity(self):
        e=Engine.__new__(Engine)
        e.know=Knowledge([Item(name='Potato',ing='Potato',next='Chopped_Potato'),Item(name='Chopped_Potato',ing='Potato'),
                          Item(name='Chopped_NuggetChicken',ing='NuggetChicken')])
        e._preposed_ok=Mock(return_value=False)
        e.step_benched=Mock(return_value=False)
        e.handoff_live=Mock(return_value=False)
        op=Op('pass','Potato',handoff='cook')
        self.assertTrue(e._op_actionable(None,{},op,0,0,'Chopped_Potato')[0])
        self.assertFalse(e._op_actionable(None,{},op,0,0,'Chopped_NuggetChicken')[0])
        self.assertFalse(e._handoff_matches('Chopped_Potato',Op('pass','Potato',handoff='chop')))
        self.assertFalse(e._handoff_matches('Potato',op))

    def test_potato_and_pancake_are_not_cooking_vessels(self):
        from map_model import is_pot
        for name in ('Potato','ChoppedPotato','PotatoCooked','Pancake'):
            self.assertFalse(is_pot(name))
            self.assertFalse(is_pot(name,'Pre-Ingredient'))
        for name, tag in [('utensil_pot_01',''),('FryingPan (1)',''),('FrierBasket','CookingUtensil')]:
            self.assertTrue(is_pot(name,tag))

    def test_split_kitchen_bots_meet_before_discarding_ingredients(self):
        for sender in (True, False):
            with self.subTest(sender=sender):
                e = Engine.__new__(Engine)
                e.pos = Mock(return_value=(0, 0, 'Potato' if sender else ''))
                e._mate = Mock(return_value=(0, 10, '' if sender else 'Potato', 10, False))
                own = {(0,0):0, (0,3):3}
                other = {(0,10):0, (0,7):3}
                tm = SimpleNamespace(ok=True, distances_from=Mock(side_effect=[own, other]),
                                     world_of=lambda x,z:(x,z))
                e.terrain = Mock(return_value=tm)
                e._travel_edges = Mock(return_value=[])
                e._pass_candidates = Mock(return_value=[Op('pass','Potato')])
                e._op_target_for_score = Mock(return_value=((0,2),'board'))
                e._stand_cell_of = Mock(return_value=(0,3))
                e.navigate_smart = Mock(return_value=True)
                e.wait_idle = Mock(); e.log = Mock(); e.face = Mock()
                self.assertTrue(e._rendezvous_handoff(None, {}, None, [Op('chop','Potato')], [0]))
                e.navigate_smart.assert_called_once_with(None,0,3,tight=.4,replans=1)
                if not sender:
                    e.navigate_smart.reset_mock()
                    e._mate.return_value=(0,4,'Potato',0,False)
                    tm.distances_from.side_effect=[own,other]
                    self.assertTrue(e._rendezvous_handoff(None,{},None,[Op('chop','Potato')],[0]))
                    e.navigate_smart.assert_not_called()
                    e.face.assert_called_once_with(0,4)
                    e.wait_idle.assert_called_once_with(.35)
                # Human play and unrelated held objects must not trigger this.
                e._mate.return_value=(0,10,'' if sender else 'Potato',10,True)
                self.assertFalse(e._rendezvous_handoff(None,{},None,[Op('chop','Potato')],[0]))
                e._mate.return_value=(0,10,'',10,False)
                e.pos.return_value=(0,0,'Plate 5')
                self.assertFalse(e._rendezvous_handoff(None,{},None,[Op('chop','Potato')],[0]))

    def test_plated_cucumber_is_not_a_loose_cucumber_source(self):
        from map_model import KitchenMap
        item=SimpleNamespace(name='Plated_Cucumber',tag='Plate',x=0,z=0,on='counter',carrier='')
        km=KitchenMap(items=[item])
        self.assertEqual(km.unseen_items(),[])
        item.on=''
        e=Engine.__new__(Engine)
        self.assertIsNone(e._find_ground_item(km,'Cucumber',0,0))
        item.name='Cucumber';item.tag='Ingredient'
        self.assertIsNotNone(e._find_ground_item(km,'Cucumber',0,0))

    def test_repeated_impacts_do_not_disable_escape_movement(self):
        e=Engine.__new__(Engine); e.is_impacted=Mock(return_value=True)
        with patch('engine.time.monotonic',side_effect=[10,10.3,11,20,21]):
            self.assertTrue(e._wait_for_initial_impact({}))
            self.assertTrue(e._wait_for_initial_impact({}))
            self.assertFalse(e._wait_for_initial_impact({}))
            self.assertFalse(e._wait_for_initial_impact({}))
            e.is_impacted.return_value=False
            self.assertFalse(e._wait_for_initial_impact({}))
            e.is_impacted.return_value=True
            self.assertTrue(e._wait_for_initial_impact({}))
    def test_recovery_fetch_cannot_ping_pong_held_ingredients(self):
        e=Engine.__new__(Engine)
        for flag in ('prep','redo'):
            op=Op('fetch','Cucumber');setattr(op,flag,True)
            for strict in (True,False):
                self.assertFalse(e._op_actionable(None,{},op,0,0,'Lettuce',strict=strict)[0])
                self.assertTrue(e._op_actionable(None,{},op,0,0,'',strict=strict)[0])

    def test_foreign_salad_does_not_skip_lettuce_preparation(self):
        e=Engine.__new__(Engine)
        flow=DishFlow('Salad_Plain',ops=[Op('assemble','Lettuce')])
        self.assertTrue(e._step_group_ok(flow,{'lettuce','tomato'},'Lettuce')[0])
        self.assertFalse(e._step_group_ok(flow,{'lettuce'},'Lettuce')[0])

    def test_foreign_plate_cannot_win_cached_spot_selection(self):
        from map_model import KitchenMap, Station
        e=Engine.__new__(Engine); e.board=None;e.cid=0;e._assemble_sid='bad'
        e._branch_ok=Mock(return_value=True);e._mate=Mock(return_value=None)
        e._mate_near=Mock(return_value=False);e.state=Mock(return_value={})
        e._plate_contents_on=lambda s: {'lettuce','tomato'} if s.id=='bad' else set()
        e._has_plate=lambda s:s.id=='bad'
        bad=Station(id='bad',kind='WorkSurface',sub='',name='bad',x=0,z=0)
        good=Station(id='good',kind='WorkSurface',sub='',name='good',x=1,z=0)
        km=SimpleNamespace(of=lambda _: [bad,good],stations={'bad':bad,'good':good},nearest=lambda *a:None)
        flow=DishFlow('Salad_Plain',ops=[Op('assemble','Lettuce')])
        self.assertEqual(e.pick_assemble_spot(km,0,0,flow=flow).id,'good')
        # A carried chopped ingredient used to skip the selector entirely,
        # keeping the incompatible plate from the previous order.
        e.assemble_spot=bad;e.map=Mock(return_value=km)
        e.state.return_value={'inRound':True};e.pos=Mock(return_value=(0,0,'ChoppedLettuce'))
        self.assertTrue(e._prepare_plate(flow))
        self.assertEqual(e.assemble_spot.id,'good')

    def test_washing_is_exclusive_and_released_after_exception(self):
        from team import OrderBoard
        board=OrderBoard(); e=Engine.__new__(Engine);e.board=board;e.cid=1;e.log=Mock()
        e._op_wash_owned=Mock(side_effect=RuntimeError('test'))
        board.claim_stove('chore:wash',0)
        self.assertFalse(e.op_wash(None,{}));e._op_wash_owned.assert_not_called()
        board.release_stove('chore:wash',0)
        with self.assertRaises(RuntimeError):e.op_wash(None,{})
        self.assertIsNone(board.stove_owner('chore:wash'))

    def test_wash_finishes_when_teammate_takes_clean_plates(self):
        e=Engine.__new__(Engine);e.log=Mock()
        dirty=SimpleNamespace(id='dirty0',name='DirtyPlateStack',n=2,x=0,z=0)
        sink=SimpleNamespace(id='wash0',name='WashingPart',x=0,z=0,on=[])
        km=SimpleNamespace(stations={'wash0':sink},of=lambda sem:{'dirty_plates':[dirty],'wash':[sink]}.get(sem,[]))
        e.map=Mock(return_value=km);e.pos=Mock(return_value=(0,0,'DirtyPlateStack'))
        e.state=Mock(return_value={'inRound':True})
        e.interaction_targets=Mock(side_effect=[('WashingPart','WashingPart'),('WashingPart',''),('WashingPart',''),('WashingPart',''),('WashingPart','')])
        e._approach=Mock(return_value=True);e.interact=Mock(return_value=True);e.face=Mock()
        e.kb=SimpleNamespace(b={'chop':'TEST'},chop=Mock())
        with patch('bridge.keyboard_input.key_down'),patch('bridge.keyboard_input.key_up'),patch('engine.time.sleep'),patch('engine.time.time',side_effect=[0,0,1,2,6]):
            self.assertTrue(e.op_wash(km,{'inRound':True},budget=5))

    def test_clean_stack_approach_uses_its_drying_station(self):
        e = Engine.__new__(Engine)
        e._station_named = Mock(return_value=None)
        e.state = Mock(return_value=None)
        e.navigate_smart = Mock(return_value=False)
        mount = SimpleNamespace(name='DryingPart', on=['CleanPlateStack'], x=19.6, z=2.4)
        km = SimpleNamespace(stations={'return_plates1': mount})
        e._approach(km, 19.6, 2.4, want='CleanPlateStack')
        e._station_named.assert_called_once_with(km, 'DryingPart', at=(19.6,2.4))

    def test_source_prefers_real_pasta_even_when_tomato_is_closer(self):
        from map_model import KitchenMap, Station
        pasta = Station(id='crate0', kind='PickupItemSpawner', sub='', name='far', x=10, z=0, spawn='Pasta')
        tomato = Station(id='crate1', kind='PickupItemSpawner', sub='', name='near', x=0, z=0, spawn='PastaTomato')
        km = KitchenMap(stations={'crate0':pasta, 'crate1':tomato})
        self.assertIs(km.find_source('Pasta'), pasta)
        self.assertIsNone(km.find_source('Pasta', ok=lambda s:s is tomato))

    def test_wash_two_dirty_plates_does_not_stop_after_first_clean_plate(self):
        e = Engine.__new__(Engine)
        e.log = Mock()
        dirty = SimpleNamespace(id='dirty_plates0', name='DirtyPlateStack', n=2, x=0, z=0)
        sink = SimpleNamespace(id='wash0', name='WashingPart', x=0, z=0, on=[])
        km = SimpleNamespace(stations={'wash0':sink}, of=lambda sem: {'dirty_plates':[dirty], 'wash':[sink]}.get(sem, []))
        clean1 = SimpleNamespace(of=lambda sem: [SimpleNamespace(n=1)] if sem=='plates' else [])
        clean2 = SimpleNamespace(of=lambda sem: [SimpleNamespace(n=2)] if sem=='plates' else [])
        e.map = Mock(side_effect=[km, clean1, km, clean2])
        e.pos = Mock(side_effect=[(0,0,''), (0,0,''), (0,0,'DirtyPlateStack'), (0,0,''), (0,0,'')])
        e.state = Mock(return_value={'inRound':True})
        e.interaction_targets = Mock(return_value=('WashingPart','WashingPart'))
        e._approach = Mock(return_value=True)
        e.interact = Mock(return_value=True)
        e.face = Mock()
        e.kb = SimpleNamespace(b={'chop':'TEST'}, chop=Mock())
        with patch('bridge.keyboard_input.key_down'), patch('bridge.keyboard_input.key_up'), patch('engine.time.sleep'):
            self.assertTrue(e.op_wash(km, {'inRound':True}))
        self.assertEqual(e.kb.chop.call_count, 2)

    def test_pasta_tomato_is_not_pasta_for_fetch_or_advancement(self):
        e = Engine.__new__(Engine)
        self.assertFalse(e._held_is('PastaTomato', 'Pasta'))
        self.assertFalse(e._held_is_norm('PastaTomato', 'Pasta'))
        self.assertTrue(e._held_is('Pasta (2)', 'Pasta'))

    def test_exact_duplicate_counter_name_must_also_be_near_target(self):
        e = Engine.__new__(Engine)
        e.log = Mock()
        e.interaction_targets = Mock(return_value=('counter', ''))
        e.handle_target = Mock(return_value='counter')
        e.pos = Mock(return_value=(1, 1, ''))
        e._near_chef = Mock(return_value=True)
        km = SimpleNamespace(stations={'a':SimpleNamespace(name='counter'), 'b':SimpleNamespace(name='counter')})
        self.assertFalse(e._aim_ok({}, 'counter', at=(20, 1), km=km))
        self.assertTrue(e._aim_ok({}, 'counter', at=(1, 2), km=km))

    def test_dirty_plates_and_stacks_are_not_serving_plates(self):
        from map_model import is_plate
        e = Engine.__new__(Engine)
        for name in ('DirtyPlate', 'DirtyPlateStack', 'CleanPlateStack'):
            self.assertFalse(is_plate(name))
            self.assertFalse(e._is_plate(name))
            from map_model import Station
            s = Station(id='counter0', kind='AttachStation', sub='', name='counter', x=0, z=0, on=[name])
            self.assertFalse(e._has_plate(s))
            self.assertEqual(s.empty_plate_names(), [])
        self.assertTrue(is_plate('equipment_plate_01'))

    def test_no_clean_plates_prioritizes_washing_over_more_prep(self):
        import scoring
        self.assertGreaterEqual(scoring.wash_urgency(0, 1), 60)
        self.assertEqual(scoring.wash_urgency(1, 1), 0)
        self.assertEqual(scoring.wash_urgency(0, 0), 0)

    def test_off_heat_cooked_pot_does_not_trigger_emergency_rescue(self):
        e = Engine.__new__(Engine)
        km = SimpleNamespace(cooking=[SimpleNamespace(need=12, prog=20, kind='cook', x=1,z=2)],
                             of=lambda sem: [])
        self.assertEqual(e._rescues(km, {}), [])

    def test_duplicate_station_names_do_not_select_a_distant_pot(self):
        from map_model import KitchenMap
        km = KitchenMap.__new__(KitchenMap)
        km.stations = {}
        near = SimpleNamespace(name='utensil_pot_01', tag='CookingUtensil', on='counter', x=1, z=2)
        far = SimpleNamespace(name='utensil_pot_02', tag='CookingUtensil', on='counter', x=20, z=2)
        km.items = [far, near]
        self.assertIs(km.pot_on(SimpleNamespace(name='counter', x=1,z=2)), near)

    def test_cook_uses_prepared_ingredient_identity(self):
        e = Engine.__new__(Engine)
        e.know = Knowledge([Item(name='PastaTomato', ing='PastaTomato', next='PastaTomato'),
                            Item(name='ChoppedPastaTomato', ing='PastaTomato', cook_steps=[42])])
        self.assertTrue(e._held_cookable('ChoppedPastaTomato', 'PastaTomato'))
        self.assertFalse(e._held_cookable('PastaTomato', 'PastaTomato'))
        self.assertFalse(e._held_cookable('ChoppedPastaTomato', 'Pasta'))
        self.assertEqual(e._cook_req('PastaTomato'), ('', [42]))

    def test_pot_only_fire_is_extinguished_and_fresh_state_ends_loop(self):
        e = Engine.__new__(Engine)
        e.log = Mock()
        e.state = Mock(return_value={})
        e.map = Mock(return_value=object())
        e.pos = Mock(return_value=(0, 0, 'utensil_fire_extinguisher_01'))
        e._my_player_index = Mock(return_value=0)
        e._approach = Mock(return_value=True)
        e.face = Mock()
        e.bridge = SimpleNamespace(direct=Mock(return_value={'ok':True}))
        fire = {'name':'pot', 'type':'CookingBurn', 'x':1, 'z':0}
        e._fire_targets = Mock(side_effect=[[fire], [fire], [], []])
        e.fires = Mock(return_value=[])
        with patch('engine.time.sleep'):
            self.assertEqual(e.extinguish(object(), {}), 1)
        self.assertEqual(e.bridge.direct.call_count, 2)
        e.fires.assert_not_called()

    def test_burnt_cleanup_never_discards_uncooked_or_good_food(self):
        e = Engine.__new__(Engine)
        e.pos = Mock(return_value=(0, 0, ''))
        e._fire_targets = Mock(return_value=[])
        km = SimpleNamespace(cooking=[SimpleNamespace(state='Cooked', burning=False, inside='Pasta')])
        self.assertFalse(e.clear_burnt_pot(km, {}))

    def test_burnt_cooking_flag_is_not_a_live_flame(self):
        e = Engine.__new__(Engine)
        e.bridge = SimpleNamespace(get_dyn=Mock(return_value={'fires':[]}))
        km = SimpleNamespace(cooking=[SimpleNamespace(burning=True, state='Burnt', x=1, z=2)])
        self.assertEqual(e._fire_targets(km), [])

    def test_pasta_does_not_match_tomato_pot(self):
        e = Engine.__new__(Engine)
        e.board = None
        stove = SimpleNamespace(id='hob0')
        ck = SimpleNamespace(is_pot=True, inside='PastaTomato')
        km = SimpleNamespace(sorted_by_dist=lambda *a: [stove], cooking_on=lambda s: ck)
        self.assertEqual(e._find_pot_with(km, 0, 0, 'Pasta'), (None, None))
        ck.inside = 'Pasta (2)'
        self.assertEqual(e._find_pot_with(km, 0, 0, 'Pasta'), (stove, ck))
        ck.state = 'Burnt'
        self.assertEqual(e._find_pot_with(km, 0, 0, 'Pasta'), (None, None))

    def test_cooking_deadline_uses_elapsed_progress(self):
        e = Engine.__new__(Engine)
        e.log = Mock()
        ck = SimpleNamespace(need=12, prog=9, burn_at=24, burning=False, inside='Pasta')
        stove = SimpleNamespace(id='hob0')
        with patch('engine.time.time', return_value=100), patch('engine.COOK_LEAVE', True):
            self.assertTrue(e._pot_put(object(), stove, 'pot', Op('cook', 'Pasta'), ck, ''))
            self.assertEqual(e._pots['hob0']['due'], 103)
            import engine
            self.assertEqual(e._pots['hob0']['late'], 115 - engine.COOK_RETURN_MARGIN)
            e._pots.clear()
            ck.burning = True
            self.assertFalse(e._pot_put(object(), stove, 'pot', Op('cook', 'Pasta'), ck, ''))
            ck.burning = False
            ck.inside = 'PastaTomato'
            self.assertFalse(e._pot_put(object(), stove, 'pot', Op('cook', 'Pasta'), ck, ''))

    def test_cook_returns_to_stove_before_waiting_and_rejects_burnt_food(self):
        for state in ('Cooked', 'Burnt'):
            with self.subTest(state=state):
                e = Engine.__new__(Engine)
                e.state = Mock(return_value={'inRound': True})
                e.pos = Mock(return_value=(0, 0, 'plate'))
                stove = SimpleNamespace(id='hob0', x=4, z=5, pot_name=lambda: 'pot')
                ck = SimpleNamespace(state=state, burning=False, ready=state == 'Cooked',
                                     inside='Pasta', prog=13, need=12)
                km = SimpleNamespace(cooking_on=lambda s: ck)
                e.map = Mock(return_value=km)
                e._find_pot_with = Mock(return_value=(stove, ck))
                e._pot_get = Mock(return_value=(None, None))
                e._pot_put = Mock(return_value=False)
                e._get_plate_for_pot = Mock(return_value=True)
                events = []
                e.navigate_smart = Mock(side_effect=lambda *a, **k: events.append('return') or True)
                e.round_active = Mock(side_effect=lambda: events.append('wait') or True)
                e._take_from_pot = Mock(return_value=True)
                e._pot_done = Mock()
                e.log = Mock()
                self.assertEqual(e._cook(km, 0, 0, Op('cook','Pasta',in_pot=True), {}), state == 'Cooked')
                self.assertEqual(events[:2], ['return', 'wait'])
                self.assertEqual(e._take_from_pot.call_count, int(state == 'Cooked'))

    def test_empty_plate_does_not_remove_recipe_steps(self):
        e = Engine.__new__(Engine)
        spot = SimpleNamespace(id='counter0', on=['equipment_plate_01'])
        e._spot_for = Mock(return_value=spot)
        e.state = Mock(return_value={})
        e._plate_contents_on = Mock(return_value=set())
        ops = [Op('fetch','Pasta'), Op('cook','Pasta'), Op('assemble','Pasta'), Op('deliver','Dish')]
        self.assertEqual(e._skip_already_on_spot(SimpleNamespace(name='Dish'), ops), ops)

    def test_complete_dish_in_hand_goes_straight_to_delivery(self):
        e = Engine.__new__(Engine)
        e.pos = Mock(return_value=(0,0,'equipment_plate_01'))
        e._dish_sets = Mock(return_value=({'pasta','pastatomato'},set()))
        e._held_contents = Mock(return_value={'pasta','pastatomato'})
        e._deliver_plate = Mock(return_value=True)
        e._put_down_plate = Mock(side_effect=AssertionError('Do not put down a complete dish'))
        self.assertTrue(e.op_deliver(None,0,0,Op('deliver','Dish'),{},object()))
        e._deliver_plate.assert_called_once()

    def test_duplicate_station_names_resolve_by_position(self):
        wrong = SimpleNamespace(id='counter1',name='counter (2)',x=20.8,z=2.3)
        right = SimpleNamespace(id='counter4',name='counter (2)',x=14.4,z=-2.4)
        km = SimpleNamespace(stations={'counter1':wrong,'counter4':right})
        self.assertIs(Engine._station_named(km,'counter (2)',at=(14.4,-2.4)),right)

    def test_ground_pasta_does_not_match_chopped_tomato(self):
        e = Engine.__new__(Engine)
        e._unclaimed_items = Mock(return_value=[SimpleNamespace(name='ChoppedPastaTomato',tag='Ingredient',x=0,z=0)])
        e._ground_ok = Mock(return_value=True)
        self.assertIsNone(e._find_ground_item(None,'Pasta',0,0))
        self.assertIsNone(e._find_ground_item(None,'PastaTomato',0,0))

    def test_approach_refreshes_moving_station_before_navigation(self):
        e = Engine.__new__(Engine)
        old = SimpleNamespace(id='counter4',name='counter (2)',x=4,z=5)
        new = SimpleNamespace(id='counter4',name='counter (2)',x=8,z=9)
        e.state = Mock(return_value={'inRound':True})
        e.map = Mock(return_value=SimpleNamespace(stations={'counter4':new}))
        e.pos = Mock(return_value=(None,None,''))
        e.navigate_smart = Mock(return_value=False)
        self.assertFalse(e._approach(SimpleNamespace(stations={'counter4':old}),4,5,want='counter (2)'))
        self.assertEqual(e.navigate_smart.call_args.args[1:3], (8,9))

    def test_unspawned_conveyor_fish_still_requires_chopping(self):
        kb = Knowledge([Item(name='SushiFish', prefab=True, next='SushiFish', stages=8, x=99, z=99)])
        detail = {'name':'Sushi_PlainFish', 'plate':'Plate', 'tree':{'k':'ing','n':'SushiFish'}}
        flow = derive(detail, kb)
        self.assertEqual([o.action for o in flow.ops], ['fetch','chop','assemble','deliver'])
        self.assertEqual(flow.ops[1].chop_stages, 8)
        self.assertEqual((flow.ops[0].at_name,flow.ops[0].at_x,flow.ops[0].at_z), ('',0,0))

    def test_existing_processed_fish_does_not_require_chopping(self):
        kb = Knowledge([Item(name='SushiFish',prefab=True,next='SushiFish',stages=8),
                        Item(name='ChoppedSushiFish',ing='SushiFish')])
        self.assertEqual(resolve_leaf(kb,'SushiFish')['kind'],'ready')

    def test_extra_prep_does_not_block_current_processing(self):
        e = Engine.__new__(Engine)
        e._preposed_ok = lambda _: False
        e.step_benched = lambda _: False
        e.handoff_live = lambda *args: False
        ops = [Op('mix','Flour'),Op('fetch','Flour',prep=True)]
        km = SimpleNamespace(nearest=lambda *args: object())
        self.assertTrue(e._op_actionable(km, {}, ops[0], 0, 0, 'Flour', idx=0,ops=ops,pending=[0,1])[0])
        ops[1].prep=False
        # A later portion is not a prerequisite even if it is a recipe step.
        self.assertTrue(e._op_actionable(km, {}, ops[0], 0, 0, 'Flour', idx=0,ops=ops,pending=[0,1])[0])
        ops = [Op('fetch','Flour'),Op('mix','Flour')]
        self.assertFalse(e._op_actionable(km, {}, ops[1], 0, 0, 'Flour', idx=1,ops=ops,pending=[0,1])[0])

    def test_pause_then_stop_inside_an_unfinished_order(self):
        e = Engine.__new__(Engine)
        e.kb = SimpleNamespace(release_all=Mock())
        e.state = Mock(return_value={'inRound':True})
        e._publish_status = Mock()
        e.map = Mock(side_effect=AssertionError('No planning or driving while paused'))
        e._ctrl_stop = False
        e._ctrl_paused = False
        e._handoff_flow = ''
        e._handoffs = {}
        e._handoff_told = set()
        e._handoff_seen = {}
        calls = []
        def commands():
            calls.append(True)
            if len(calls) == 1:
                e._ctrl_paused = True
            else:
                e._ctrl_stop = True
        e.apply_commands = commands
        with patch('engine.time.sleep'):
            self.assertFalse(e._execute_scored(DishFlow('test'), [Op('fetch','SushiFish')], 2, 1))
        self.assertEqual(len(calls),2)
        e._publish_status.assert_called_once_with({'inRound':True},paused=True)
        e.map.assert_not_called()
        self.assertEqual(e.kb.release_all.call_count,2)

    def test_diagonal_belt_waiting_spot_attempts_pickup(self):
        e = Engine.__new__(Engine)
        e.terrain = lambda: None
        e.round_active = lambda: True
        e.state = Mock(return_value={'inRound': True})
        e.pos = Mock(side_effect=[(13.8,4.8,''),(13.8,3.6,'Seaweed')])
        e._find_item_station = lambda *args: SimpleNamespace(x=12.8,z=3.6)
        e._approach = Mock(return_value=True)
        e.interact = Mock(return_value=True)
        self.assertTrue(e._grab_from_belt(None,Op('fetch','Seaweed'),13.8,4.8))
        e._approach.assert_called_once()
        e.interact.assert_called_once_with('pickup',verify_hold_change=True)

if __name__=='__main__':
    unittest.main()
