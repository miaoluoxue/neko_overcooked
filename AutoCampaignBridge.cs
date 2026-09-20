using System;
using System.Collections.Generic;
using System.Net;
using System.Net.Sockets;
using System.IO;
using System.Reflection;
using System.Reflection.Emit;
using System.Text;
using System.Threading;
using BepInEx;
using UnityEngine;
using UnityEngine.UI;
using HarmonyLib;

[BepInPlugin("local.neko.campaign", "Neko Campaign Bridge", "0.2.0")]
[BepInProcess("Overcooked2.exe")]
public class AutoCampaignBridge : BaseUnityPlugin
{
    class Job { public string Command; public string Result; public ManualResetEvent Done = new ManualResetEvent(false); public bool Cancelled; }
    readonly Queue<Job> jobs = new Queue<Job>();
    TcpListener listener;
    volatile bool running;
    bool background;
    float lease;
    static bool automaticInput;
    Harmony harmony;
    static void AllowBackgroundMenu(ref bool __result) { if(automaticInput)__result=true; }
    public static bool MenuHasFocus() { return automaticInput || Application.isFocused; }
    static IEnumerable<CodeInstruction> EngagementFocus(IEnumerable<CodeInstruction> codes) {
        foreach(var code in codes) {
            var method=code.operand as MethodInfo;
            if(method!=null && method.DeclaringType==typeof(Application) && method.Name=="get_isFocused") {code.opcode=OpCodes.Call;code.operand=typeof(AutoCampaignBridge).GetMethod("MenuHasFocus");}
            yield return code;
        }
    }
    static BindingFlags flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
    static object Field(object o, string name) { if(o==null)return null; for(Type t=o.GetType();t!=null;t=t.BaseType){FieldInfo f=t.GetField(name,flags|BindingFlags.DeclaredOnly);if(f!=null)return f.GetValue(o);}return null; }
    static object Call(object o,string name,params object[] args) { return o.GetType().GetMethod(name,flags).Invoke(o,args); }
    static string Q(object s) { return "\""+(s==null?"":s.ToString()).Replace("\\","\\\\").Replace("\"","\\\"").Replace("\r","\\r").Replace("\n","\\n").Replace("\t","\\t")+"\""; }
    static bool Active(Component c) {return c!=null && c.gameObject.activeInHierarchy;}
    static bool Visible(object o) {var c=o as Component;var g=o as GameObject;return c!=null?Active(c):g!=null&&g.activeInHierarchy;}
    static Component[] Components() {return UnityEngine.Object.FindObjectsOfType<Component>();}
    static Component Find(string type) {foreach(var c in Components())if(c.GetType().Name==type)return c;return null;}
    static Component FindMenu(string type) {foreach(var c in Resources.FindObjectsOfTypeAll<Component>())if(c.GetType().Name==type && c.gameObject.scene.IsValid())return c;return null;}
    void Awake() { background=Application.runInBackground;harmony=new Harmony("local.neko.campaign");var logical=AccessTools.TypeByName("LogicalButtonBase");harmony.Patch(AccessTools.Method(logical,"CanProcessInput"),postfix:new HarmonyMethod(typeof(AutoCampaignBridge).GetMethod("AllowBackgroundMenu",BindingFlags.Static|BindingFlags.NonPublic)));harmony.Patch(AccessTools.Method(AccessTools.TypeByName("LogicalPCEngagementButton"),"IsDown"),transpiler:new HarmonyMethod(typeof(AutoCampaignBridge).GetMethod("EngagementFocus",BindingFlags.Static|BindingFlags.NonPublic)));running=true;listener=new TcpListener(IPAddress.Loopback,48779);listener.Start();new Thread(Accept){IsBackground=true}.Start();Logger.LogInfo("Campaign bridge listening on localhost:48779"); }
    void Accept() { while(running)try {var client=listener.AcceptTcpClient();new Thread(delegate(){Serve(client);}){IsBackground=true}.Start();}catch {if(!running)return;} }
    void Serve(TcpClient client) {using(client)try {client.ReceiveTimeout=15000;client.SendTimeout=15000;using(var stream=client.GetStream())using(var rd=new StreamReader(stream,Encoding.UTF8))using(var wr=new StreamWriter(stream,new UTF8Encoding(false))){wr.AutoFlush=true;string line;while(running&&(line=rd.ReadLine())!=null){if(line.Length>256)break;var j=new Job{Command=line};lock(jobs){jobs.Enqueue(j);}if(!j.Done.WaitOne(10000,false)){lock(j){j.Cancelled=true;}wr.WriteLine("{\"error\":\"main thread timeout\"}");}else wr.WriteLine(j.Result);}}}catch{} }
    void Update() { automaticInput=lease>Time.realtimeSinceStartup;if(automaticInput)Application.runInBackground=true;else Application.runInBackground=background;Job j=null;lock(jobs){if(jobs.Count>0)j=jobs.Dequeue();}if(j==null)return;lock(j){if(j.Cancelled)return;try{j.Result=Execute(j.Command);}catch(Exception e){j.Result="{\"error\":"+Q(e.InnerException==null?e.Message:e.InnerException.Message)+"}";}j.Done.Set();} }
    void OnDestroy(){running=false;automaticInput=false;if(listener!=null)listener.Stop();Application.runInBackground=background;}
    string Execute(string command)
    {
        if(command=="capabilities")return "{\"version\":\"0.2.0\",\"quit\":true,\"arcade\":true}";
        if(command=="status") {lease=Time.realtimeSinceStartup+20;return Status();}
        if(command=="stop"){lease=0;return "{\"ok\":true}";}
        var a=command.Split(':');
        // Same game entry as InGamePauseMenu.OnQuitConfirmed (IL_003b..0047).
        // GameUtils.QuitLevel preserves campaign progress and returns through its real flow.
        if(command=="quit") {
            var pause=Find("InGamePauseMenu");
            if(pause!=null)Call(pause,"OnQuitConfirmed");
            else {var mp=Find("MultiplayerController");if(mp!=null)Call(mp,"StopSynchronisation");GameUtils.QuitLevel();}
            return "{\"ok\":true}";
        }
        // FrontendCoopTabOptions.OnCouchPlayClicked creates the local cooperative arcade session.
        if(command=="arcade-menu") {var c=FindMenu("FrontendCoopTabOptions");var root=Find("FrontendRootMenu");if(c==null||root==null)throw new Exception("Coop menu unavailable");Call(root,"OpenFrontendMenu",c);return "{\"ok\":true}";}
        if(command=="arcade") {var c=Find("FrontendCoopTabOptions");if(c==null)throw new Exception("Coop menu unavailable");Call(c,"OnCouchPlayClicked");return "{\"ok\":true}";}
        if(command=="arcade-ready") {
            var lobby=Find("ServerLobbyFlowController");if(lobby==null)throw new Exception("Arcade lobby unavailable");
            var method=lobby.GetType().GetMethod("SelectTheme",flags);
            var et=method.GetParameters()[0].ParameterType;
            object random=null;foreach(string n in Enum.GetNames(et))if(n.IndexOf("random",StringComparison.OrdinalIgnoreCase)>=0)random=Enum.Parse(et,n);
            if(random==null)throw new Exception("Random theme unavailable");
            // SelectTheme uses the game's own AllUsersSelected/state transition and countdown.
            var choices=Field(lobby,"m_userChoices") as Array;
            if(choices==null)throw new Exception("Lobby users not ready");
            for(int i=0;i<choices.Length;i++)method.Invoke(lobby,new object[]{random,i});
            return "{\"ok\":true}";
        }
        if(a[0]=="campaign") {
            var c=Find("FrontendCampaignTabOptions");if(c==null)throw new Exception("Campaign menu unavailable");
            var root=Field(c,"m_frontendRootMenu");Call(root,"OpenFrontendMenu",c);
            return "{\"ok\":true}";
        }
        if(a[0]=="continue") {var c=Find("FrontendCampaignTabOptions");if(c==null||!Visible(Field(c,"m_continueButton")))throw new Exception("No continue button");Call(c,"OnContinueGameClicked");return "{\"ok\":true}";}
        if(a[0]=="new") {var c=Find("FrontendCampaignTabOptions");if(!Active(c))throw new Exception("No campaign menu");Call(c,"OnNewGameClicked",new object[]{null});return "{\"ok\":true}";}
        if(a[0]=="empty" && a.Length==2) {int id=int.Parse(a[1]);foreach(var c in Components())if(c.GetInstanceID()==id&&c.GetType().Name=="SaveSlotElement"&&Active(c)&&Visible(Field(c,"m_noSaveSlot"))){Call(c,"OnSlotClicked");return "{\"ok\":true}";}throw new Exception("Not an empty active save slot");}
        if(a[0]=="click"&&a.Length==2){int id=int.Parse(a[1]);foreach(var b in UnityEngine.Object.FindObjectsOfType<Button>())if(b.GetInstanceID()==id&&Active(b)&&b.IsInteractable()){b.onClick.Invoke();return "{\"ok\":true}";}throw new Exception("Button no longer available");}
        if(a[0]=="level"&&a.Length==2){int id=int.Parse(a[1]);var flow=Find("WorldMapFlowController");var avatar=Find("MapAvatarControls");var server=Find("ServerWorldMapFlowController");if(flow==null||avatar==null||server==null)throw new Exception("World map not ready");foreach(var c in Components())if(c.GetInstanceID()==id&&(c is LevelPortalMapNode||c is MiniLevelPortalMapNode)){if(!(bool)Call(flow,"IsLevelUnlocked",c))throw new Exception("Level locked");if(c is MiniLevelPortalMapNode)Call(server,"OnSelectMiniLevelPortal",avatar,c,0);else Call(server,"OnSelectLevelPortal",avatar,c);return "{\"ok\":true}";}throw new Exception("Unknown level");}
        throw new Exception("Unknown command");
    }
    string Status()
    {
        var s=new StringBuilder("{\"scene\":");s.Append(Q(UnityEngine.SceneManagement.SceneManager.GetActiveScene().name));s.Append(",\"buttons\":[");bool first=true;
        foreach(var b in UnityEngine.Object.FindObjectsOfType<Button>())if(Active(b)&&b.IsInteractable()) {if(!first)s.Append(',');first=false;s.Append("{\"id\":"+b.GetInstanceID()+",\"name\":"+Q(b.name)+",\"text\":"+Q(string.Join(" ",Array.ConvertAll(b.GetComponentsInChildren<Text>(),delegate(Text t){return t.text;})))+",\"events\":[");for(int i=0;i<b.onClick.GetPersistentEventCount();i++){if(i>0)s.Append(',');s.Append(Q(b.onClick.GetPersistentMethodName(i)));}s.Append("]}");}
        s.Append("],\"menus\":[");first=true;foreach(var c in Components()){if(!Active(c))continue;var enabled=Field(c,"m_bMenuIsEnabled");if(enabled is bool && (bool)enabled){if(!first)s.Append(',');first=false;s.Append(Q(c.GetType().Name));}}s.Append("],\"emptySlots\":[");first=true;
        foreach(var c in Components())if(c.GetType().Name=="SaveSlotElement"&&Active(c)&&Visible(Field(c,"m_noSaveSlot"))){if(!first)s.Append(',');first=false;s.Append(c.GetInstanceID());}
        s.Append("],\"continueAvailable\":");var campaign=Find("FrontendCampaignTabOptions");s.Append(campaign!=null&&Visible(Field(campaign,"m_continueButton"))?"true":"false");s.Append(",\"levels\":[");first=true;var flow=Find("WorldMapFlowController");
        // ServerMiniLevelPortalMapNode.OnAllowedSelection IL_0038..0041 uses variant 0.
        // Include story portals: their completion is part of IsLevelChainComplete.
        if(flow!=null)foreach(var c in Components())if(c is LevelPortalMapNode||c is MiniLevelPortalMapNode){var node=(PortalMapNode)c;int index=node.LevelIndex;if(index<0)continue;if(!first)s.Append(',');first=false;object progress=GameUtils.GetGameSession().Progress.SaveData.GetLevelProgress(index);s.Append("{\"id\":"+c.GetInstanceID()+",\"index\":"+index+",\"story\":"+(c is MiniLevelPortalMapNode?"true":"false")+",\"name\":"+Q(c.name)+",\"unlocked\":"+((bool)Call(flow,"IsLevelUnlocked",c)?"true":"false")+",\"progress\":{");bool pf=true;if(progress!=null)foreach(var f in progress.GetType().GetFields(flags)){if(!pf)s.Append(',');pf=false;s.Append(Q(f.Name)+":"+Q(f.GetValue(progress)));}s.Append("}}");}
        s.Append("]}");return s.ToString();
    }
}
