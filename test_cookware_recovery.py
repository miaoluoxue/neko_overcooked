import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as N
sys.path.insert(0,str(Path(__file__).parent/'neko'))
from cookware_recovery import recovery_plan

class CookwareTests(unittest.TestCase):
    def setup_map(self):
        self.pan=N(name='pan',is_pot=True,busy=False,state='Raw',station='Hob',x=5,z=0)
        self.counter=N(id='counter',kind='AttachStation',on=['pan'],n=1,x=5,z=0,active=True)
        self.stove=N(id='hob',kind='CookingStation',on=[],n=0,x=0,z=0,sub='Hob',active=True)
        return N(cooking=[self.pan],stations={'counter':self.counter,'hob':self.stove})
    def test_parked_empty_pan_returns_to_compatible_stove(self):
        km=self.setup_map()
        self.assertEqual(recovery_plan(km,0,0),(self.pan,self.counter,self.stove))
    def test_busy_wrong_type_occupied_claimed_and_carried_food_are_untouched(self):
        km=self.setup_map();self.pan.busy=True
        self.assertIsNone(recovery_plan(km,0,0));self.pan.busy=False
        self.stove.sub='Oven';self.assertIsNone(recovery_plan(km,0,0));self.stove.sub='Hob'
        self.stove.on=['other'];self.assertIsNone(recovery_plan(km,0,0));self.stove.on=[]
        self.assertIsNone(recovery_plan(km,0,0,available=lambda k:False))
        self.assertIsNone(recovery_plan(km,0,0,held='ChoppedTomato'))
    def test_held_pan_can_resume_but_unmounted_unheld_pan_is_not_guessed(self):
        km=self.setup_map();self.counter.on=[]
        self.assertIsNone(recovery_plan(km,0,0))
        self.assertEqual(recovery_plan(km,0,0,held='pan'),(self.pan,None,self.stove))
