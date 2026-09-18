import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { GhostPublisherSettings } from "@/components/connect/ghost-publisher-select";
import { CreateConnectionDialog } from "@/components/connection-create-dialog";
import { api } from "@/lib/api";
import type { ConnectorInfo } from "@/lib/types";
vi.mock("@/lib/api",async original=>({...await original<typeof import("@/lib/api")>(),api:vi.fn()}));
afterEach(()=>{cleanup();vi.clearAllMocks();});
const agents=[{id:"writer",name:"Blogger",role_title:"Writer",status:"active"},{id:"director",name:"Ava",role_title:"Marketing Director",status:"active"},{id:"inactive",name:"Former Director",status:"disabled"}];
const field={name:"publisher_agent_id",label:"Publishing agent ID",kind:"string",required:false,default:null,help:"Only this agent can publish.",placeholder:"",minimum:null,maximum:null,auth_types:[]};
const connector={connector_type:"ghost",display_name:"Ghost",auth_schemes:[{type:"api_key",label:"Admin key",description:"",secret_fields:[]}],config_fields:[field]} as unknown as ConnectorInfo;
function wrapper(node:React.ReactNode){render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}})}>{node}</QueryClientProvider>);}
it("offers named workspace agents and drafts only on manual connection setup",async()=>{
 vi.mocked(api).mockResolvedValue(agents);
 wrapper(<CreateConnectionDialog workspaceId="ws" connector={connector} onClose={vi.fn()} onCreated={vi.fn()}/>);
 const select=await screen.findByRole("combobox",{name:"Publishing director"});
 await screen.findByRole("option",{name:"Ava — Marketing Director"});
 expect(screen.getByRole("option",{name:"Drafts only"})).toBeDefined();expect(screen.queryByRole("option",{name:/Former Director/})).toBeNull();
 expect(screen.queryByRole("textbox",{name:"Publishing agent ID"})).toBeNull();
 fireEvent.change(select,{target:{value:"director"}});expect(select).toHaveProperty("value","director");
 expect(api).toHaveBeenCalledWith("/api/v1/workspaces/ws/agents");
});
it("renders the same named selection in a catalog schema",async()=>{
 vi.mocked(api).mockResolvedValue(agents);
 wrapper(<CreateConnectionDialog workspaceId="ws" connector={connector} onClose={vi.fn()} onCreated={vi.fn()} schema={{version:1,connector_type:"ghost",auth:{type:"none",note:""},degraded:[],fields:[{...field,type:"string",secret:false,enum:[],max_length:null,multiline:false}]}}/>);
 await screen.findByRole("option",{name:"Ava — Marketing Director"});expect(screen.getByRole("combobox",{name:"Publishing director"})).toBeDefined();
});
it("updates an existing publisher and can explicitly return it to drafts only",async()=>{
 vi.mocked(api).mockImplementation(async(_url,options)=>options?.method?{}:agents);
 wrapper(<GhostPublisherSettings workspaceId="ws" connection={{id:"connection",config_json:{admin_url:"https://blog.example",publisher_agent_id:"director"}}} canManage/>);
 const select=await screen.findByRole("combobox",{name:"Publishing director"});await screen.findByRole("option",{name:"Ava — Marketing Director"});expect(select).toHaveProperty("value","director");
 fireEvent.change(select,{target:{value:""}});fireEvent.click(screen.getByRole("button",{name:"Save publishing assignment"}));
 await waitFor(()=>expect(api).toHaveBeenCalledWith("/api/v1/workspaces/ws/connections/connection/config",expect.objectContaining({method:"PATCH",body:{config:{admin_url:"https://blog.example",publisher_agent_id:""}}})));
});
it("preserves an unavailable director and gives viewers no update action",async()=>{
 vi.mocked(api).mockResolvedValue(agents);
 wrapper(<GhostPublisherSettings workspaceId="ws" connection={{id:"connection",config_json:{publisher_agent_id:"inactive"}}} canManage={false}/>);
 await screen.findByRole("option",{name:"Former Director (disabled)"});
 expect(screen.getByRole("combobox",{name:"Publishing director"})).toHaveProperty("value","inactive");
 expect(screen.getByRole("combobox",{name:"Publishing director"})).toHaveProperty("disabled",true);
 expect(screen.queryByRole("button",{name:"Save publishing assignment"})).toBeNull();
});
