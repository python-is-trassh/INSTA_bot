#!/bin/bash

# Enhanced Instagram Bot - Automatic Installation Script
# This script sets up the Enhanced Instagram Bot with all dependencies

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Function to check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Function to check Python version
check_python_version() {
    print_status "Checking Python version..."
    
    if command_exists python3; then
        PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
        PYTHON_CMD="python3"
    elif command_exists python; then
        PYTHON_VERSION=$(python -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
        PYTHON_CMD="python"
    else
        print_error "Python is not installed!"
        exit 1
    fi
    
    # Check if version is 3.7 or higher
    if ! python3 -c "import sys; exit(0 if sys.version_info >= (3, 7) else 1)" 2>/dev/null; then
        print_error "Python 3.7 or higher is required. Found: $PYTHON_VERSION"
        exit 1
    fi
    
    print_success "Python $PYTHON_VERSION found"
}

# Function to install system dependencies
install_system_deps() {
    print_status "Installing system dependencies..."
    
    # Detect OS
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        # Linux
        if command_exists apt-get; then
            # Ubuntu/Debian
            sudo apt-get update
            sudo apt-get install -y python3-pip python3-venv ffmpeg sqlite3
        elif command_exists yum; then
            # CentOS/RHEL
            sudo yum install -y python3-pip python3-venv ffmpeg sqlite
        elif command_exists pacman; then
            # Arch Linux
            sudo pacman -S python-pip ffmpeg sqlite
        else
            print_warning "Unknown Linux distribution. Please install python3-pip, ffmpeg, and sqlite manually."
        fi
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS
        if command_exists brew; then
            brew install python ffmpeg sqlite
        else
            print_warning "Homebrew not found. Please install Python, FFmpeg, and SQLite manually."
        fi
    else
        print_warning "Unknown OS. Please install Python, FFmpeg, and SQLite manually."
    fi
}

# Function to create virtual environment
create_virtual_env() {
    print_status "Creating virtual environment..."
    
    if [ ! -d "venv" ]; then
        $PYTHON_CMD -m venv venv
        print_success "Virtual environment created"
    else
        print_warning "Virtual environment already exists"
    fi
    
    # Activate virtual environment
    source venv/bin/activate
    
    # Upgrade pip
    pip install --upgrade pip
}

# Function to install Python dependencies
install_python_deps() {
    print_status "Installing Python dependencies..."
    
    if [ -f "requirements.txt" ]; then
        pip install -r requirements.txt
        print_success "Python dependencies installed"
    else
        print_error "requirements.txt not found!"
        exit 1
    fi
}

# Function to setup configuration
setup_config() {
    print_status "Setting up configuration..."
    
    if [ ! -f ".env" ]; then
        if [ -f ".env.example" ]; then
            cp .env.example .env
            print_success "Configuration file created from template"
            print_warning "Please edit .env file with your settings!"
        else
            print_error ".env.example not found!"
            exit 1
        fi
    else
        print_warning ".env file already exists"
    fi
}

# Function to create directories
create_directories() {
    print_status "Creating necessary directories..."
    
    mkdir -p tmp logs
    print_success "Directories created"
}

# Function to setup database
setup_database() {
    print_status "Setting up database..."
    
    # Activate virtual environment
    source venv/bin/activate
    
    # Create database tables
    $PYTHON_CMD database_utils.py create
    print_success "Database initialized"
}

# Function to generate encryption key
generate_encryption_key() {
    print_status "Generating secure encryption key..."
    
    # Generate a random 32-character key
    ENCRYPTION_KEY=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-32)
    
    # Update .env file
    if grep -q "ENCRYPTION_PASSWORD=" .env; then
        sed -i.bak "s/ENCRYPTION_PASSWORD=.*/ENCRYPTION_PASSWORD=$ENCRYPTION_KEY/" .env
    else
        echo "ENCRYPTION_PASSWORD=$ENCRYPTION_KEY" >> .env
    fi
    
    print_success "Encryption key generated and saved to .env"
}

# Function to test installation
test_installation() {
    print_status "Testing installation..."
    
    # Activate virtual environment
    source venv/bin/activate
    
    # Test imports
    if $PYTHON_CMD -c "
import sys
sys.path.append('.')
try:
    from config import get_config
    from enhanced_insta_bot import EnhancedInstagramBot
    print('✅ All modules imported successfully')
except ImportError as e:
    print(f'❌ Import error: {e}')
    sys.exit(1)
"; then
        print_success "Installation test passed"
    else
        print_error "Installation test failed"
        exit 1
    fi
}

# Function to show setup instructions
show_instructions() {
    print_success "Installation completed successfully!"
    echo
    print_status "Next steps:"
    echo "1. Edit the .env file with your Telegram bot token:"
    echo "   nano .env"
    echo
    echo "2. Get your Telegram bot token from @BotFather"
    echo "3. Add your Telegram user ID to ALLOWED_USERS"
    echo "4. Customize other settings as needed"
    echo
    echo "5. Activate the virtual environment:"
    echo "   source venv/bin/activate"
    echo
    echo "6. Run the bot:"
    echo "   python enhanced_insta_bot.py"
    echo
    print_status "Useful commands:"
    echo "• Database backup: python database_utils.py backup"
    echo "• Database stats: python database_utils.py stats"
    echo "• Database cleanup: python database_utils.py cleanup"
    echo
    print_warning "Remember to set up your .env file before running the bot!"
}

# Function to show help
show_help() {
    echo "Enhanced Instagram Bot - Installation Script"
    echo
    echo "Usage: $0 [OPTIONS]"
    echo
    echo "Options:"
    echo "  --help              Show this help message"
    echo "  --skip-system-deps  Skip system dependencies installation"
    echo "  --skip-db-setup     Skip database setup"
    echo "  --dev               Install development dependencies"
    echo
    echo "Environment Variables:"
    echo "  SKIP_SYSTEM_DEPS=1  Skip system dependencies"
    echo "  SKIP_DB_SETUP=1     Skip database setup"
    echo
}

# Main installation function
main() {
    local skip_system_deps=false
    local skip_db_setup=false
    local dev_install=false
    
    # Parse command line arguments
    while [[ $# -gt 0 ]]; do
        case $1 in
            --help)
                show_help
                exit 0
                ;;
            --skip-system-deps)
                skip_system_deps=true
                shift
                ;;
            --skip-db-setup)
                skip_db_setup=true
                shift
                ;;
            --dev)
                dev_install=true
                shift
                ;;
            *)
                print_error "Unknown option: $1"
                show_help
                exit 1
                ;;
        esac
    done
    
    # Check for environment variables
    if [ "$SKIP_SYSTEM_DEPS" = "1" ]; then
        skip_system_deps=true
    fi
    
    if [ "$SKIP_DB_SETUP" = "1" ]; then
        skip_db_setup=true
    fi
    
    echo "🤖 Enhanced Instagram Bot - Installation Script"
    echo "=============================================="
    echo
    
    # Check if running as root (not recommended)
    if [ "$EUID" -eq 0 ]; then
        print_warning "Running as root is not recommended!"
        read -p "Continue anyway? (y/N): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
    
    # Start installation
    print_status "Starting Enhanced Instagram Bot installation..."
    
    # Check Python version
    check_python_version
    
    # Install system dependencies
    if [ "$skip_system_deps" = false ]; then
        install_system_deps
    else
        print_warning "Skipping system dependencies installation"
    fi
    
    # Create virtual environment
    create_virtual_env
    
    # Install Python dependencies
    install_python_deps
    
    # Setup configuration
    setup_config
    
    # Create directories
    create_directories
    
    # Generate encryption key
    generate_encryption_key
    
    # Setup database
    if [ "$skip_db_setup" = false ]; then
        setup_database
    else
        print_warning "Skipping database setup"
    fi
    
    # Test installation
    test_installation
    
    # Show final instructions
    show_instructions
}

# Error handling
trap 'print_error "Installation failed at line $LINENO"' ERR

# Run main function
main "$@"
