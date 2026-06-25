/**
 * Admin API: user management, instance config.
 */
import { fetchJSON, jsonBody } from '$lib/apis';

export interface AdminUser {
	user_id: string;
	username: string;
	display_name: string | null;
	profile_image_url: string | null;
	role: string;
	created_at: number;
}

export const listUsers = async (): Promise<AdminUser[]> => {
	const data = await fetchJSON<{ users: AdminUser[] }>('/api/admin/users');
	return data.users;
};

export const createUser = (username: string, password: string, role = 'user') =>
	fetchJSON('/api/admin/users', jsonBody({ username, password, role }));

export const deleteUser = (userId: string) =>
	fetchJSON(`/api/admin/users/${userId}`, { method: 'DELETE' });

export const updateRole = (userId: string, role: string) =>
	fetchJSON(`/api/admin/users/${userId}/role`, {
		...jsonBody({ role }),
		method: 'PUT'
	});

export const updateUserProfile = (userId: string, display_name: string | null) =>
	fetchJSON(`/api/admin/users/${userId}/profile`, {
		...jsonBody({ display_name }),
		method: 'PUT'
	});

export const resetPassword = (userId: string, password: string) =>
	fetchJSON(`/api/admin/users/${userId}/password`, {
		...jsonBody({ password }),
		method: 'PUT'
	});

export const updateUsername = (userId: string, username: string) =>
	fetchJSON(`/api/admin/users/${userId}/username`, {
		...jsonBody({ username }),
		method: 'PUT'
	});

export const getAdminConfig = async (): Promise<Record<string, unknown>> => {
	const data = await fetchJSON<{ config: Record<string, unknown> }>('/api/admin/config');
	return data.config;
};

export const updateConfig = (config: Record<string, unknown>) =>
	fetchJSON('/api/admin/config', {
		...jsonBody({ config }),
		method: 'PUT'
	});

// ── Connections ─────────────────────────────────────────────

export interface Connection {
	id: string;
	name: string;
	provider: string;
	api_type: string;
	prefix_id: string | null;
	base_url: string | null;
	api_key: string | null;
	enabled: boolean;
	data: { models?: string[] };
}

export const listConnections = async (): Promise<Connection[]> => {
	const data = await fetchJSON<{ connections: Connection[] }>('/api/admin/connections');
	return data.connections;
};

export const createConnection = (conn: {
	name: string;
	provider: string;
	api_type?: string;
	prefix_id?: string | null;
	base_url?: string | null;
	api_key?: string | null;
	enabled?: boolean;
	models?: string[];
}) => fetchJSON('/api/admin/connections', jsonBody(conn));

export const updateConnection = (
	id: string,
	updates: Partial<Omit<Connection, 'id' | 'data'>> & { models?: string[] }
) =>
	fetchJSON(`/api/admin/connections/${id}`, {
		...jsonBody(updates),
		method: 'PUT'
	});

export const deleteConnection = (id: string) =>
	fetchJSON(`/api/admin/connections/${id}`, { method: 'DELETE' });

export const verifyConnection = (id: string) =>
	fetchJSON<{ ok: boolean; message: string }>(`/api/admin/connections/${id}/verify`, {
		method: 'POST'
	});

// ── Model Config ────────────────────────────────────────────

export interface ModelConfigEntry {
	is_active?: boolean;
	params?: {
		request_params?: Record<string, unknown>;
		system_prompt?: string;
		compact_token_threshold?: number;
	};
}

export interface ModelConfigResponse {
	config: Record<string, ModelConfigEntry>;
	models: { id: string; name: string; provider: string; connection_id: string }[];
}

export const getModelConfig = async (): Promise<ModelConfigResponse> =>
	fetchJSON<ModelConfigResponse>('/api/admin/models/config');

export const refreshModelList = async (): Promise<ModelConfigResponse> =>
	fetchJSON<ModelConfigResponse>('/api/admin/models/refresh', { method: 'POST' });

export const updateModelConfig = (
	modelId: string,
	update: { is_active?: boolean; params?: Record<string, unknown> }
) =>
	fetchJSON(`/api/admin/models/${encodeURIComponent(modelId)}/config`, {
		...jsonBody(update),
		method: 'PUT'
	});

// ── Tool Servers ────────────────────────────────────────────

export interface ToolServer {
	id: string;
	type: 'openapi' | 'mcp' | 'mcp_stdio';
	url: string;
	path: string;
	auth_type: string;
	key: string;
	name: string;
	description: string;
	headers: Record<string, string> | null;
	enabled: boolean;
	// Stdio MCP fields
	command?: string;
	args?: string[];
	env?: Record<string, string> | null;
	cwd?: string | null;
}

export const listToolServers = async (): Promise<ToolServer[]> => {
	const data = await fetchJSON<{ servers: ToolServer[] }>('/api/admin/tools/servers');
	return data.servers;
};

export const createToolServer = (server: Omit<ToolServer, 'id'>) =>
	fetchJSON('/api/admin/tools/servers', jsonBody(server));

export const updateToolServer = (id: string, updates: Partial<Omit<ToolServer, 'id'>>) =>
	fetchJSON(`/api/admin/tools/servers/${id}`, {
		...jsonBody(updates),
		method: 'PUT'
	});

export const deleteToolServer = (id: string) =>
	fetchJSON(`/api/admin/tools/servers/${id}`, { method: 'DELETE' });

export const verifyToolServer = (id: string) =>
	fetchJSON<{ ok: boolean; tools?: { name: string; description: string }[]; message?: string }>(
		`/api/admin/tools/servers/${id}/verify`,
		{ method: 'POST' }
	);
