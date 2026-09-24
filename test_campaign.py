import unittest
from run_campaign import menu_action, choose_level, kitchen_run_key

class CampaignTests(unittest.TestCase):
    def test_story_portal_before_hidden_challenge_and_completed_story_not_replayed(self):
        main=dict(id=6,index=6,name='Text.Menu.Level06',unlocked=True,progress={'ScoreStars':'2','Completed':'True'})
        hidden=dict(id=37,index=37,name='Text.Menu.KevinLevel01',unlocked=True,progress={'ScoreStars':'0'})
        story=dict(id=50,index=50,story=True,unlocked=True,progress={'Completed':'False','ScoreStars':'0'})
        self.assertEqual(choose_level([main,hidden,story])['id'],50)
        story['progress']['Completed']='True'
        self.assertEqual(choose_level([main,hidden,story])['id'],6)
    def test_replaced_kitchen_invalidates_engine_but_movement_does_not(self):
        state={'scene':'dynamic','round':{'seq':7},'layout':{'stations':[{'iid':11,'kind':'PlateStation','x':0}]}}
        before=kitchen_run_key(state)
        state['layout']['stations'][0]['x']=20
        self.assertEqual(kitchen_run_key(state),before)
        state['layout']['stations'].append({'iid':9,'kind':'CleanPlateStack'})
        self.assertEqual(kitchen_run_key(state),before)
        state['layout']['stations'][0]['iid']=12
        self.assertNotEqual(kitchen_run_key(state),before)
        state['layout']['stations']=[]
        self.assertEqual(kitchen_run_key(state,before),before)

    def test_never_overwrite_save(self):
        self.assertIsNone(menu_action({"menus":["SelectSaveDialog"],"emptySlots":[]},{"scene":"StartScreen"}))
        self.assertEqual(menu_action({"menus":["SelectSaveDialog"],"emptySlots":[12]},{"scene":"StartScreen"}),"empty:12")

    def test_continue_existing(self):
        self.assertEqual(menu_action({"menus":["FrontendCampaignTabOptions"],"continueAvailable":True},{"scene":"StartScreen"}),"continue")

    def test_no_locked_level(self):
        self.assertIsNone(choose_level([{"id":1,"index":1,"unlocked":False}]))
        self.assertEqual(choose_level([{"id":2,"index":2,"unlocked":True,"progress":{"stars":"0"}},{"id":1,"index":1,"unlocked":True,"progress":{"stars":"3"}}])["id"],2)

    def test_no_arbitrary_dialog_accept(self):
        self.assertIsNone(menu_action({"menus":[],"buttons":[]},{"mode":"Campaign","app":{"dialogs":True}}))
        self.assertIsNone(menu_action({"menus":[],"buttons":[{"events":["OnConfirmOverwrite"]}]},{"mode":"Campaign"}))

if __name__=="__main__":unittest.main()
