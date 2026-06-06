/**
 * Files API: browse, read, write, move, delete, upload, archive, search.
 */
import { fetchHandler, fetchJSON, jsonBody } from '$lib/apis';

export const listDir = (path: string) =>
	fetchJSON<{ entries: unknown[] }>(`/api/workspace/files?path=${encodeURIComponent(path)}`);

export const readFile = (path: string) =>
	fetchHandler(`/api/workspace/files/read?path=${encodeURIComponent(path)}`);

export const readFileText = async (path: string): Promise<string> => {
	const res = await readFile(path);
	if (!res.ok) throw new Error(`Failed to read ${path}: ${res.status}`);
	return res.text();
};

export const writeFile = (path: string, content: string) =>
	fetchJSON('/api/workspace/files/write', jsonBody({ path, content }));

export const deleteFiles = (paths: string[]) =>
	Promise.all(paths.map((path) => fetchJSON('/api/workspace/files/delete', jsonBody({ path }))));

export const moveFile = (src: string, dest: string) =>
	fetchJSON('/api/workspace/files/move', jsonBody({ source: src, destination: dest }));

export const createEntry = (path: string, type: 'file' | 'directory') =>
	fetchJSON('/api/workspace/files/create', jsonBody({ path, type }));

export const uploadFiles = (path: string, form: FormData) =>
	fetchHandler('/api/workspace/files/upload', { method: 'POST', body: form });

export const uploadFile = (form: FormData) =>
	fetchJSON<{ id: string; url: string }>('/api/files', { method: 'POST', body: form });

export const downloadArchive = (paths: string[]) =>
	fetchHandler('/api/workspace/files/archive', { method: 'POST', ...jsonBody({ paths }) });

export const searchFiles = (query: string, path: string) =>
	fetchJSON(
		`/api/workspace/files/search?query=${encodeURIComponent(query)}&path=${encodeURIComponent(path)}`
	);
