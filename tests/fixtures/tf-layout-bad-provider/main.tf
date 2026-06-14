provider "aws" {
  region = "us-east-1"
}

terraform {
  backend "s3" {
    bucket = "my-state"
    key    = "state.tfstate"
  }
}

resource "aws_instance" "example" {
  ami           = "ami-123456"
  instance_type = "t2.micro"
}
