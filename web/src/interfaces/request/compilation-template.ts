import {
  CompilationTemplateConfig,
  CompilationTemplateGroupScope,
  CompilationTemplateKind,
} from '../database/compilation-template';
import { IPaginationRequestBody } from './base';

/**
 * One template payload as embedded inside a group create/update request.
 */
export interface ICompilationTemplatePayload {
  name: string;
  description?: string;
  kind: CompilationTemplateKind;
  config: CompilationTemplateConfig;
}

export interface IListCompilationTemplateGroupsRequest extends IPaginationRequestBody {
  scope?: CompilationTemplateGroupScope;
}

export interface ICreateCompilationTemplateGroupRequest {
  name: string;
  description?: string;
  templates: ICompilationTemplatePayload[];
}

export interface IUpdateCompilationTemplateGroupRequest extends Partial<ICreateCompilationTemplateGroupRequest> {
  id: string;
}
