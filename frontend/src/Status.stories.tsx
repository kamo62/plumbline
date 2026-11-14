import type { Meta, StoryObj } from '@storybook/react';
import { CircleAlert, CircleHelp, CheckCircle2 } from 'lucide-react';
function StatusExamples() { return <div style={{display:'grid',gap:16}}><span className="status"><CheckCircle2 size={16}/>Complete</span><span className="status"><CircleAlert size={16}/>Partial</span><span className="status"><CircleHelp size={16}/>Unknown</span></div>; }
const meta: Meta<typeof StatusExamples> = { title: 'Runs/Completeness', component: StatusExamples };
export default meta;
export const States: StoryObj<typeof StatusExamples> = {};
