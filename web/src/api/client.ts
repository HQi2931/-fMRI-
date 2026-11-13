import { agentApi } from "./agent";
import { literatureApi } from "./literature";
import { environmentApi } from "./environment";
import { workspacesApi } from "./workspaces";
import { projectsApi } from "./projects";
import { plansApi } from "./plans";
import { runsApi } from "./runs";
import { statisticsApi } from "./statistics";

export type * from "./types";

export const api = { ...agentApi, ...literatureApi, ...environmentApi, ...workspacesApi, ...projectsApi, ...plansApi, ...runsApi, ...statisticsApi };
export { ApiError, describeError } from "./transport";
export type { ConversationStreamEvent } from "./transport";
