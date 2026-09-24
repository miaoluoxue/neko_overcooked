import unittest
import time
from unittest.mock import Mock, patch
from run_tutorial import Relay, assembly_counter, plate_contents
from run_agent_api import health

class RecoveryTests(unittest.TestCase):
    def test_stack_is_not_assembly_counter(self):
        counter=dict(kind='AttachStation',x=20.4,z=-8.4)
        stack=dict(kind='CleanPlateStack',x=14.4,z=-8.4,ontags=['Plate'])
        self.assertIs(assembly_counter([stack,counter]),counter)

    def test_partial_dish_does_not_fetch_duplicate(self):
        relay=Relay.__new__(Relay);relay.plate={};relay.fresh=Mock(return_value=dict(ontags=['Plate'],onhas=['Lettuce']))
        relay.stations=[]
        relay.ingredient('Lettuce') # Would fail looking for a crate if recovery were removed.
        self.assertEqual(plate_contents(dict(ontags=['Plate'],onhas=['Lettuce+Tomato'])),{'Lettuce','Tomato'})

    def test_player_mapping_ignores_enumeration_order(self):
        relay=Relay.__new__(Relay)
        relay.state=lambda:dict(layout=dict(chefs=[dict(player='Two',id=0),dict(player='One',id=1)]))
        self.assertEqual(relay.chef(0)['id'],1)

    def test_stale_and_stopped_supervisor_not_ready(self):
        for status in (dict(at=time.time()-20),dict(at=time.time(),running=False)):
            bus=Mock();bus.get.return_value=status
            self.assertFalse(health(bus)['ready'])
        bus.get.return_value=dict(at=time.time(),running=True)
        self.assertTrue(health(bus)['ready'])

    def test_tutorial_publishes_both_player_states(self):
        relay=Relay.__new__(Relay);relay.bus=Mock();relay.bus.claim.return_value=None
        relay.b=Mock();relay.published=0;relay.actions={0:dict(action='chop',target='Tomato')}
        relay.b.get_state.return_value=dict(inRound=True,layout=dict(chefs=[dict(player='Two',id=0),dict(player='One',id=1)]))
        with patch('run_tutorial.control.take',return_value=[]):relay.state()
        calls=relay.bus.put.call_args_list
        self.assertEqual([c.args[0] for c in calls],['engine:P1','engine:P2'])
        self.assertEqual(calls[0].args[1]['chef_id'],1)
        self.assertEqual(calls[0].args[1]['action']['target'],'Tomato')

if __name__=='__main__':unittest.main()
