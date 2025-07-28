#!/bin/bash
set -e

# Load environment variables from .env file
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

# Check required environment variables
if [ -z "$DEPLOY_USER" ] || [ -z "$DEPLOY_HOST" ] || [ -z "$DEPLOY_PATH" ]; then
  echo "Error: Missing required environment variables"
  echo "Please ensure DEPLOY_USER, DEPLOY_HOST, and DEPLOY_PATH are set in .env"
  exit 1
fi

SERVER="$DEPLOY_USER@$DEPLOY_HOST"
REMOTE_PATH="$DEPLOY_PATH"

echo "🚀 Deploying to $SERVER..."

# Use rsync with sudo on the remote side
rsync -avz --delete \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'venv' \
  --exclude '.env' \
  --exclude 'uv.lock' \
  --exclude '.DS_Store' \
  --exclude '*.db' \
  --rsync-path="sudo rsync" \
  . $SERVER:$REMOTE_PATH/

echo "🔧 Setting up environment and restarting..."
ssh $SERVER "
  cd $REMOTE_PATH
  
  # Install/update Python dependencies
  echo '📦 Installing Python dependencies...'
  sudo -u www-data python3 -m venv venv
  sudo -u www-data venv/bin/pip install -r requirements.txt
  
  # Fix file permissions
  sudo chown -R www-data:www-data $REMOTE_PATH
  
  # Restart service
  sudo systemctl restart airq
"

echo "🎉 Deployment complete!"
echo ""
echo "📖 Next steps on server:"
echo "   1. SSH to server: ssh $SERVER"
echo "   2. Initialize database: cd $REMOTE_PATH && sudo -u www-data venv/bin/flask init-db"
echo "   3. Add devices: sudo -u www-data venv/bin/flask device add airgradient \"Device Name\" --token TOKEN --location LOCATION --validate"
echo "   4. List devices: sudo -u www-data venv/bin/flask device list"