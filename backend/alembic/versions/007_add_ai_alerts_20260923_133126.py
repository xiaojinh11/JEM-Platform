from alembic import op
import sqlalchemy as sa

revision = '20260923_133126'
down_revision = 'd4201e9b8fae'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        'ai_alerts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source_kind', sa.String(32), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('severity', sa.String(16), nullable=False),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('extra', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('tokens_used', sa.Integer(), nullable=False),
        sa.Column('duration_ms', sa.Integer(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('chat_message_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['chat_message_id'], ['ai_chat_messages.id'], ondelete='SET NULL'),
        sa.Index('ix_ai_alerts_source', 'source_kind', 'source_id', unique=True),
        sa.Index('ix_ai_alerts_created_at', 'created_at'),
        sa.Index('ix_ai_alerts_severity', 'severity'),
    )

def downgrade() -> None:
    op.drop_table('ai_alerts')

