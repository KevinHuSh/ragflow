import { IListCompilationTemplateGroupsRequest } from '@/interfaces/request/compilation-template';
import api from '@/utils/api';
import request from '@/utils/request';

/**
 * Service layer for knowledge compilation templates.
 *
 * Saved entities are now *groups* — a group contains one artifacts template
 * (dataset scope) OR N non-artifacts templates (file scope). The
 * individual-template CRUD endpoints have been removed in favor of group
 * CRUD; the only template-level endpoint that remains is ``/builtins``,
 * which serves the read-only YAML defaults used to pre-fill each child
 * slot in the "Add template group" panel.
 */
const compilationTemplateService = {
  // group CRUD
  listGroups: (params?: IListCompilationTemplateGroupsRequest) =>
    request.get(api.listCompilationTemplateGroups, { params }),
  getGroup: (params: { id: string }) =>
    request.get(api.getCompilationTemplateGroup(params.id)),
  createGroup: (params?: Record<string, any>) =>
    request.post(api.createCompilationTemplateGroup, { data: params }),
  updateGroup: ({ id, ...params }: Record<string, any>) =>
    request.put(api.updateCompilationTemplateGroup(id), { data: params }),
  deleteGroup: ({ id }: { id: string }) =>
    request.delete(api.deleteCompilationTemplateGroup(id)),
  // builtin template palette (used as pre-fill in the group editor)
  builtins: () => request.get(api.listBuiltinCompilationTemplates),
};

export default compilationTemplateService;
